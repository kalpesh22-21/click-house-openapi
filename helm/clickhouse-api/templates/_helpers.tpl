{{/*
Expand the name of the chart.
*/}}
{{- define "clickhouse-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited.
*/}}
{{- define "clickhouse-api.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label value.
*/}}
{{- define "clickhouse-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every resource.
*/}}
{{- define "clickhouse-api.labels" -}}
helm.sh/chart: {{ include "clickhouse-api.chart" . }}
{{ include "clickhouse-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — used in Deployment/Service selectors.
*/}}
{{- define "clickhouse-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "clickhouse-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "clickhouse-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "clickhouse-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret used by the Deployment.
Returns the existingSecret name when configured, otherwise the chart-managed secret name.
An existing Secret must contain all six keys with their canonical env-var names:
  CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_DATABASE,
  CLICKHOUSE_SECURE, CLICKHOUSE_PASSWORD
*/}}
{{- define "clickhouse-api.secretName" -}}
{{- if .Values.secrets.existingSecret.name }}
{{- .Values.secrets.existingSecret.name }}
{{- else }}
{{- include "clickhouse-api.fullname" . }}
{{- end }}
{{- end }}
