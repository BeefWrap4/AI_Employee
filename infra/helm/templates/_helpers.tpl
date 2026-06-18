{{/*
Common labels for all AI Employee resources.
*/}}
{{- define "ai-employee.labels" -}}
app.kubernetes.io/part-of: ai-employee
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Render a service block (ConfigMap + optional PVC + Deployment + Service).
Usage: pass the service dict and name.
*/}}
{{- define "ai-employee.serviceName" -}}
{{- .name -}}
{{- end -}}
