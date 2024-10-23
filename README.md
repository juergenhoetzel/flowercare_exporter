Xiaomi MiFlora JSON/Prometheus/Graphite Exporter
================================================

# Export targets

  * JSON
  * [Grafana Graphite metrics](https://grafana.com/docs/grafana-cloud/send-data/metrics/metrics-graphite/)
  * [Prometheus pushgateway](https://github.com/prometheus/pushgateway)

# Tested Devices

  * Xiaomi Mi Flora plant sensor

# Example Usage

Push to Graphite

```bash
flowercare_exporter   -l DEBUG -t 120 \
	-a "C4:7C:8D:6C:10:9B=Alyssum" \
	-a "C4:7C:8D:6C:10:C2=Acacia" \
	-a "C4:7C:8D:6C:10:4D=Alcea" \
	-g "https://graphite-prod-13-prod-us-east-0.grafana.net/graphite/metrics"  metrics
```
Push to Prometheus pushgateway

```bash
flowercare_exporter   -l DEBUG  -t 120  \
	-a "C4:7C:8D:6C:10:9B=Alyssum" \
	-a "C4:7C:8D:6C:10:C2=Acacia" \
	-a "C4:7C:8D:6C:10:4D=Alcea" \
	-p http://herakles:9091/metrics/job/some_job  metrics
```


Blink LED to identify device
```bash
flowercare_exporter   -l DEBUG  -t 120 \
	-a "C4:7C:8D:6C:10:9B=Alyssum" \
	-a "C4:7C:8D:6C:10:C2=Acacia" \
	-a "C4:7C:8D:6C:10:4D=Alcea" \
	blink
```
