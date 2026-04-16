{{- define "tile-server.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tile-server.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "tile-server.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
