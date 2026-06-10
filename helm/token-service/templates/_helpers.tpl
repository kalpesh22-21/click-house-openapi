{{/*
Expand the name of the chart.
*/}}
{{- define "token-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "token-service.fullname" -}}
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
{{- define "token-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "token-service.labels" -}}
helm.sh/chart: {{ include "token-service.chart" . }}
{{ include "token-service.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "token-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "token-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "token-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "token-service.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret used by the Deployment.
An existing Secret must contain keys: signing-key.pem and TOKEN_ISSUER_API_KEY.
*/}}
{{- define "token-service.secretName" -}}
{{- if .Values.secrets.existingSecret.name }}
{{- .Values.secrets.existingSecret.name }}
{{- else }}
{{- include "token-service.fullname" . }}
{{- end }}
{{- end }}
