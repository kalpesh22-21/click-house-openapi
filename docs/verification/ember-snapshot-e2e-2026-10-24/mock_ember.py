"""Local HTTP mock of Ember's PHP BFF; no production Ember code is used."""
import json
from pathlib import Path
import jwt,httpx
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,StreamingResponse
root=Path(Path('/tmp/data-agent-source-e2e-path').read_text().strip())
app=FastAPI();tables={};base='http://127.0.0.1:18892'
def auth(request):
 headers={k:request.headers[k] for k in ['authorization','x-session-id'] if k in request.headers}
 try:
  claims=jwt.decode(headers['authorization'].split(' ',1)[1],(root/'public.pem').read_text(),algorithms=['RS256'],audience='clickhouse-api',issuer='source-e2e')
  return (claims['sub'],headers['x-session-id']),headers
 except Exception:raise HTTPException(401)
def table_for(owner,key):return tables.setdefault((owner,key),{'snapshot':None,'rows':[],'blocked':False,'sql':None})
def response(resp):
 return JSONResponse(status_code=resp.status_code,content=resp.json(),headers={k:resp.headers[k] for k in ['retry-after'] if k in resp.headers})
def recover(table,resp,enabled):
 body=resp.json()
 if enabled and body.get('reset_table'):
  table['rows']=[];table['blocked']=True
  if body.get('detail',{}).get('code')=='SNAPSHOT_REPLACED':
   table['snapshot']=body['snapshot'];table['blocked']=False
 return response(resp)
@app.post('/api/data-agent/query/page')
async def page(request:Request):
 owner,headers=auth(request);body=await request.json();table=table_for(owner,body['table_key'])
 if body.get('snapshot_id') and body['snapshot_id']!= (table.get('snapshot') or {}).get('snapshot_id'):
  raise HTTPException(403,'Snapshot not owned by this table/session')
 if table['sql'] is None:table['sql']=body['sql']
 async with httpx.AsyncClient(timeout=210) as client:
  if table['snapshot'] is None:
   created=await client.post(base+'/query/snapshots',headers=headers,json={'sql':table['sql']})
   if created.status_code!=201:return response(created)
   table['snapshot']=created.json()
  sid=table['snapshot']['snapshot_id'];offset=body.get('offset',0)
  resp=await client.get(f'{base}/query/snapshots/{sid}/page',headers=headers,params={'offset':offset,'limit':body.get('limit',7)})
  if resp.status_code!=200:return recover(table,resp,body.get('handle_replacement',True))
  data=resp.json()
  table['rows']=([] if offset==0 else table['rows'])+data['rows']
  return data
@app.post('/api/data-agent/query/snapshot/refresh')
async def refresh(request:Request):
 owner,headers=auth(request);body=await request.json();table=table_for(owner,body['table_key'])
 payload={'sql':table['sql']}
 source=body.get('retry_source') or (table.get('snapshot') or {}).get('snapshot_id')
 if source and not body.get('legacy'):payload['refresh_of']=source
 async with httpx.AsyncClient(timeout=210) as client:
  resp=await client.post(base+'/query/snapshots',headers=headers,json=payload)
 if resp.status_code==201:
  table['snapshot']=resp.json();table['rows']=[];table['blocked']=False
 return response(resp)
@app.get('/api/data-agent/query/snapshot/download')
async def download(request:Request):
 owner,headers=auth(request);table=table_for(owner,request.query_params['table_key'])
 if table['snapshot'] is None:raise HTTPException(404)
 sid=table['snapshot']['snapshot_id'];part=request.query_params.get('part')
 client=httpx.AsyncClient(timeout=210)
 path=f'/query/snapshots/{sid}/download?part={part}' if part else f'/query/snapshots/{sid}/download-urls'
 upstream=await client.send(client.build_request('GET',base+path,headers=headers),stream=True)
 if part and upstream.status_code==200:
  async def chunks():
   try:
    async for chunk in upstream.aiter_bytes():yield chunk
   finally:await upstream.aclose();await client.aclose()
  return StreamingResponse(chunks(),headers={k:upstream.headers[k] for k in ['content-type','content-disposition'] if k in upstream.headers})
 await upstream.aread();await upstream.aclose();await client.aclose()
 return recover(table,upstream,True) if upstream.status_code!=200 else response(upstream)
@app.get('/_test/state')
async def state(request:Request):
 owner,_=auth(request);return table_for(owner,request.query_params['table_key'])
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=18893,log_level='warning')
