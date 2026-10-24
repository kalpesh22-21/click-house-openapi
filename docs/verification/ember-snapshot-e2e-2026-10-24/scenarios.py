import asyncio,csv,io,json,sys,time,uuid
from pathlib import Path
import httpx,jwt
mode=sys.argv[1];root=Path(Path('/tmp/data-agent-source-e2e-path').read_text().strip())
B='http://127.0.0.1:18891';A='http://127.0.0.1:18892';E='http://127.0.0.1:18893'
SQL='SELECT employee_code, employee_name FROM dbpcm_datawarehouse.employee ORDER BY employee_code'
report={'store':mode,'passed':[],'compatibility_findings':[]}
def auth(user='alice'):
 sid='ember'+uuid.uuid4().hex;now=int(time.time())
 token=jwt.encode(dict(sub=user,iss='source-e2e',aud='clickhouse-api',iat=now,exp=now+3600,jti=user+'-jti',clientcode='CLIENT_A',proc_center='pc',user_name=user,session_id=sid,column_scope=[]),(root/'private.pem').read_text(),algorithm='RS256',headers={'kid':'e2e'})
 return {'Authorization':'Bearer '+token,'X-Session-Id':sid}
def ok(response,status=200):
 assert response.status_code==status,(response.status_code,response.text[:800])
 return response.json()
async def main():
 async with httpx.AsyncClient(timeout=240) as c:
  h=auth();other=auth();foreign=auth('bob');table='employees'
  async def count():return ok(await c.get(B+'/_test/counts'))['query_starts']
  async def state():return ok(await c.get(E+'/_test/state',headers=h,params={'table_key':table}))
  async def page(**extra):return await c.post(E+'/api/data-agent/query/page',headers=h,json={'table_key':table,'sql':SQL,**extra})
  async def refresh(**extra):return await c.post(E+'/api/data-agent/query/snapshot/refresh',headers=h,json={'table_key':table,**extra})
  async def change(excluded):
   ok(await c.post(B+'/_test/scope',json={'excluded':excluded}))
   ok(await c.post(A+'/session/catalog/refresh',headers=h))
  initial_counts=ok(await c.get(B+'/_test/counts'))
  before=await count();first=ok(await page());assert first['row_count']==24 and len(first['rows'])==7
  first_id=first['snapshot_id'];expected=first['rows'][:];offset=7
  while first['has_more']:
   first=ok(await page(offset=offset));expected.extend(first['rows']);offset+=len(first['rows'])
  assert len(expected)==24 and len({r[0] for r in expected})==24
  assert await count()==before+1
  report['passed'].append('lazy_create_and_all_pages_one_query_24_rows')
  await page(offset=0);assert await count()==before+1
  assert ok(await refresh(legacy=True),201)['snapshot_id']==first_id
  assert await count()==before+1
  report['compatibility_findings'].append('Existing SQL-only refresh reuses old snapshot; refresh_of is required')
  replacement=ok(await refresh(),201);assert replacement['snapshot_id']!=first_id
  repeated=ok(await refresh(retry_source=first_id),201);assert repeated==replacement
  assert await count()==before+2
  assert (await state())['rows']==[]
  report['passed'].append('refresh_of_creates_once_and_retry_reuses_replacement')
  files=ok(await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table}))['files']
  assert len(files)==5
  downloaded=[]
  for f in files:
   if mode=='s3':
    assert f['url'].startswith('http://127.0.0.1:19090/')
    response=await c.get(f['url'])
   else:
    assert not f['url'].startswith('http')
    response=await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table,'part':f['part']})
   assert response.status_code==200
   rows=list(csv.reader(io.StringIO(response.text)));assert rows[0]==['employee_code','employee_name'];downloaded+=rows[1:]
  assert downloaded==expected
  assert await count()==before+2
  report['passed'].append('five_csv_parts_24_rows_match_paged_data_without_query')
  if mode=='s3':
   streamed=await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table,'part':1})
   assert streamed.status_code==200 and len(list(csv.reader(io.StringIO(streamed.text))))==6
   report['passed'].append('s3_fallback_proxy_stream_matches_first_part')
  stolen=await c.post(E+'/api/data-agent/query/page',headers=other,json={'table_key':table,'sql':SQL,'snapshot_id':replacement['snapshot_id']})
  assert stolen.status_code==403
  for headers in (other,foreign):
   for path in ('page','download-urls','download?part=1'):
    r=await c.get(A+f"/query/snapshots/{replacement['snapshot_id']}/{path}",headers=headers)
    assert r.status_code==404,(r.status_code,r.text)
  report['passed'].append('bff_ownership_403_and_direct_agent_cross_session_user_404')
  # Scope change that still permits this query.
  await page(offset=0);before_scope=await count()
  await change(['dbpcm_warehouse.employee.department_name'])
  legacy=await page(offset=7,handle_replacement=False)
  body=ok(legacy,409);assert body['detail']['code']=='SNAPSHOT_REPLACED'
  assert (await state())['snapshot']['snapshot_id']==replacement['snapshot_id']
  report['compatibility_findings'].append('Existing Ember error-only handling does not adopt SNAPSHOT_REPLACED; must clear rows and save replacement')
  recovered=ok(await page(offset=7),409)
  assert recovered['reset_table'] and recovered['snapshot']==body['snapshot']
  state_after=await state();assert state_after['rows']==[] and state_after['snapshot']==body['snapshot']
  assert await count()==before_scope+1
  fresh=ok(await page(offset=0));assert fresh['rows']==expected[:7]
  report['passed'].append('scope_recovery_one_query_replacement_retry_no_duplicate_and_restart_offset_zero')
  # Both download entry points can discover a scope change before any bytes/URLs.
  await change([])
  download_recovery=await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table})
  assert download_recovery.status_code==409 and download_recovery.json()['detail']['code']=='SNAPSHOT_REPLACED'
  assert (await state())['rows']==[]
  assert (await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table})).status_code==200
  await change(['dbpcm_warehouse.employee.department_name'])
  part_recovery=await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table,'part':1})
  assert part_recovery.status_code==409 and part_recovery.json()['detail']['code']=='SNAPSHOT_REPLACED'
  assert 'application/json' in part_recovery.headers['content-type']
  after_part=await c.get(E+'/api/data-agent/query/snapshot/download',headers=h,params={'table_key':table,'part':1})
  assert after_part.status_code==200 and len(list(csv.reader(io.StringIO(after_part.text))))==6
  report['passed'].append('scope_recovery_on_download_urls_and_csv_part_returns_json_then_fresh_download')
  # Now the original query is forbidden: no SQL dispatch, stale rows cleared.
  before_deny=await count();await change(['dbpcm_warehouse.employee.employee_name'])
  denied=await page(offset=7);assert denied.status_code==403,(denied.status_code,denied.text)
  assert denied.json()['reset_table'] is True
  assert (await state())['rows']==[] and (await state())['blocked']
  assert await count()==before_deny
  report['passed'].append('scope_reduction_denies_original_query_before_dispatch_and_clears_old_rows')
  # Restore normal scope for separate native and mixed queries.
  await change([])
  queries={
   'native':"SELECT employee_code, sum(amount) AS earnings FROM dbpcm_warehouse.payroll WHERE register_type='EARN' GROUP BY employee_code ORDER BY employee_code",
   'mixed':"SELECT e.employee_code, e.employee_name, p.earnings FROM dbpcm_datawarehouse.employee e JOIN (SELECT employee_code, sum(amount) AS earnings FROM dbpcm_warehouse.payroll WHERE register_type='EARN' GROUP BY employee_code) p ON e.employee_code=p.employee_code ORDER BY e.employee_code"}
  result_sets={}
  for label,sql in queries.items():
   r=ok(await c.post(E+'/api/data-agent/query/page',headers=h,json={'table_key':label,'sql':sql,'limit':100}))
   assert r['row_count']==24,(label,r)
   result_sets[label]=r['rows']
  assert [(r[0],str(r[-1])) for r in result_sets['native']]==[(r[0],str(r[-1])) for r in result_sets['mixed']]
  assert [r[:2] for r in result_sets['mixed']]==expected
  report['passed'].append('native_clickhouse_and_mixed_trino_24_employees_earnings_match')
  # Exercise direct create concurrent retries using same authenticated session.
  bursth=auth();await c.post(A+'/session/catalog',headers=bursth)
  count_before=await count()
  burst=await asyncio.gather(*(c.post(A+'/query/snapshots',headers=bursth,json={'sql':SQL}) for _ in range(8)))
  assert all(r.status_code in (201,409,429) for r in burst),[(r.status_code,r.text) for r in burst]
  done=next(r.json() for r in burst if r.status_code==201)
  retry=ok(await c.post(A+'/query/snapshots',headers=bursth,json={'sql':SQL}),201)
  assert done==retry and await count()==count_before+1
  report['passed'].append('eight_concurrent_agent_requests_one_database_execution')
  report['burst_statuses']=[r.status_code for r in burst]
  # Application expiry on real stored metadata, through actual backend auth.
  expired=done['snapshot_id'];scope=ok(await c.post(B+'/session/catalog',headers=bursth))['scope_token']
  ok(await c.post(B+'/_test/expire/'+expired,headers={**bursth,'X-Catalog-Scope':scope}))
  for endpoint in ('page','download-urls','download?part=1'):
   resp=await c.get(A+f'/query/snapshots/{expired}/{endpoint}',headers=bursth)
   assert resp.status_code==410,(resp.status_code,resp.text)
  report['passed'].append('expiry_410_on_page_presign_and_stream')
  expired_refresh=ok(await c.post(A+'/query/snapshots',headers=bursth,json={'sql':SQL,'refresh_of':expired}),201)
  assert expired_refresh['snapshot_id']!=expired
  report['passed'].append('expired_but_retained_snapshot_can_be_explicitly_refreshed')
  final_counts=ok(await c.get(B+'/_test/counts'))
  report['execution_counts']={k:final_counts[k]-initial_counts[k] for k in initial_counts}
  report['earnings_sum']=sum(float(r[-1]) for r in result_sets['native'])
  report['rows']=24
  Path('/tmp/ember-snapshot-e2e/report-'+mode+'.json').write_text(json.dumps(report,indent=2)+'\n')
  print(json.dumps(report,indent=2))
asyncio.run(main())
