{{- define "game-server-fleet.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
The build a GameServer runs. Label values are capped at 63 characters and
must start and end alphanumeric, which every image tag this project uses
(dev3, a commit hash) already does.
*/}}
{{- define "game-server-fleet.buildVersion" -}}
{{- default .Values.image.tag .Values.buildVersion | trunc 63 | trimSuffix "-" -}}
{{- end -}}
