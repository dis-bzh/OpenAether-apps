# Alert delivery: what is proven, what is not

Rung: written and tested on synthetic series (`scripts/dev/test-vmrules.sh`, `scripts/dev/test-alert-delivery.py`);
**never run on a cluster**. Nothing here claims ANSSI compliance. The lab pass criteria are in `docs/monitoring-0.4.md`.

## The chain

    VMRule --vmalert--> Alertmanager --route--> receiver: chat webhook (slack)   [needs a real webhook]
                                      \--Watchdog--> receiver: heartbeat URL     [needs a real URL]

The default install has no external endpoint: the seed script writes placeholders, Alertmanager starts, alerts stay
visible in Alertmanager and Grafana, and the delivery to the outside fails by design. Always on, and
needing nothing external, are the rules that watch the chain itself (`vmrule-alerting.yaml`): VMAlert cannot reach
Alertmanager, rule evaluation errors, alert state not persisted, Alertmanager config not reloaded, Alertmanager absent.

## Heartbeat (dead-man's-switch)

`Watchdog` fires forever. Alertmanager sends it every 5 minutes to the URL in `secret/observability/alertmanager-deadmansswitch`
(`OA_DEADMANSSWITCH_URL` of the seed script). The **service** pages you when the pings stop, which covers the cluster, the
node, vmalert or Alertmanager dying, and the egress being cut: the only signal that does not need the cluster. Set its grace
period above 5 minutes (15 is a floor). One URL, any ping-on-absence service. The egress policy allows `hc-ping.com` and
`hooks.slack.com` only; **another host needs one edit of the `vmalertmanager` CiliumNetworkPolicy in your overlay** (a
FQDN rule cannot read a Secret): a known manual step, tracked with the first lab.

## Scheduled delivery test (opt-in)

Component `apps/flux/components/alert-delivery-probe`, off in every profile, for a cluster whose two secrets are real:

- `AlertDeliveryProbe` fires 10 minutes at 06:00 and 18:00 UTC and is routed (`probe="delivery"`) to a quiet chat receiver
  (one message, no "resolved").
- `AlertDeliveryFailing`: Alertmanager counted a failed notification in the last hour (chat or heartbeat).
- `AlertDeliveryNotAcknowledged` (critical): the probe fired in the last 14 hours and no notification to the chat receiver
  succeeded in that time. "Acknowledged" means the receiver answered 2xx, which is what Alertmanager counts; a muted or
  unread channel behind a working webhook is not detected.
- While `AlertDeliveryNotAcknowledged` fires, Alertmanager **inhibits the Watchdog**: the heartbeat stops and the external
  service pages you through its own channel. So a broken chat leg is reported out of band without a second URL.

Limits, stated: the failing-chat alert cannot reach the chat; an Alertmanager restart is covered because counters keep their
history in the window, but a lost counter series is not; the alert names above are tested on synthetic counters, the real
`alertmanager_notifications_*` series are UNVERIFIED until a lab reads them.

## Routing

Watchdog first (it must never be swallowed), then the probe, then `severity="critical"` (hourly repeat), the rest to chat
(12 h repeat). `scripts/dev/test-alert-delivery.py` pins this order, the secrets each receiver reads, the inhibit rule,
and the egress hosts.
