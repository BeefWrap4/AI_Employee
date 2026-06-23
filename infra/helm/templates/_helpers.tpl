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

{{/*
Truthy when a service declares persistent storage.  Kubernetes expresses
storage as a quantity string ("1Gi", "500Mi") or a bare number.  Helm's
``int`` coerces "1Gi" to 0, which previously suppressed every PVC and
volumeMount — so stateful services booted with a read-only root fs and
no PVC.  We extract the leading numeric prefix and treat >0 as "has
storage"; "0", 0, "" and unset are falsy.

Returns a non-empty string ("1") for truthy, empty for falsy.  Callers
use ``{{- if include "ai-employee.hasStorage" $svc.storage }}`` which
treats "1" as true and "" as false.
*/}}
{{- define "ai-employee.hasStorage" -}}
{{- $s := . | toString -}}
{{- $num := regexFind "^[0-9]+" $s -}}
{{- if and $num (gt (atoi $num) 0) -}}
1
{{- end -}}
{{- end -}}
