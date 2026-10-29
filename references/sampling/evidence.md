# Empirical evidence and its limits

September 10–12, 2026 T5/T6 collection: 307.87 GiB of logged successful image payload over 50.18 hours, averaging 1.75 MiB/s including interruptions and inter-batch work. Final T6 with 32 workers averaged 2.47 MiB/s over its production download span and 3.26 MiB/s over recorded download stages. Median payload writes took about 0.02 s, while median computePixels calls took 13–14 s. Request time includes remote processing, network/proxy delay and transfer.

Two randomized reversed-order rounds found 16+16 task workers 1.85× faster in accepted samples/time than sequential 16-worker execution. A later corrected T6 trial found 32 workers 44.4% faster than 16 and 10.4% faster than 24, including preparation/provenance/QA. All 768 corrected-trial samples passed QA without recorded retries. An earlier trial was invalid because the HTTP pool stayed at 16; its timings are excluded.

Two probes per route/task supported Japan s2 for T5 and Japan s for T6. The previous Japan s3 failed both representative T6 probes. Alternative-route T6 headers took 14–26 s versus 1.6–2.4 s for bodies. This small test does not establish fixed route bandwidth or independent upstream capacity.

**No reliable time-of-day recommendation is supported.** Grouping the half-hour logs by task, operational route/worker period and China Standard Time hour gives at most one observed date for any fixed configuration/hour. Configuration changes, different polygons, changing cache state, a 3¼-hour OAuth transport outage and final QA are confounded with clock time. The high approximately 4.2 MiB/s T6 interval near the final morning is a one-day observation, not evidence that this route is predictably faster then.

To test a daily pattern, repeat identical bounded endpoint probes on the same compute host and candidate routes at several hours across multiple days. Keep worker count/payload fixed, record concurrent production activity, randomize route order, and compare per-day paired effects and uncertainty. Do not delay production to a supposedly better hour based on this two-day history.

Sources: `/nfs-data0/home/lyh/workspace/RSGenLM/reports/download-review-2026-09-12/REPORT.md`; worker and VPN reports linked there. Stratified clock-hour table: `reports/metadata-expansion-2026-09-13/hour-of-day.csv` in that workspace. These are historical measurements, not guarantees.

## S2 metadata pilot — September 13, 2026

A completed Slurm pilot compared 2/4/8/16 metadata workers in reversed rounds on disjoint three-hour global S2 windows, with the HTTP pool fixed at 16. Geometric-mean validated throughput was 191/350/575/774 records/s; corresponding pooled TCP receive rates were 0.274/0.502/0.819/1.108 MiB/s. The best tested worker setting was 16, about 4.05× the 2-worker baseline. Subsequent page-size rounds retained 1,000/page (657 records/s versus 609 for 500/page). All 12 phases matched independent counts, retained 376,701 records, and passed final SQLite integrity with zero recorded request errors.

This is pilot evidence, **not a production-confirmed default**: the trial used a smaller catalog and short date windows while the live 2-worker scan continued. At 16 workers, serialized write-lock holding occupied 45–47% of worker-phase wall time, so database contention increasingly matters. CPU/RAM were not saturated. Validate longer page chains and the large destination catalog before claiming sustained speedup; retest other collection families. Metadata bandwidth and image-payload bandwidth are different performance measures.

Source: `/nfs-data0/home/lyh/workspace/RSGenLM/reports/metadata-tuning-2026-09-13/REPORT.md`, with raw phases and selection alongside it.
