# Alert delivery: what is proven, what is not

Rung: written and tested on synthetic series (`scripts/dev/test-vmrules.sh`, `scripts/dev/test-alert-delivery.py`), then
**read once on a cluster in lab 35** (2026-11-07; Scaleway, 3 CP + 6 W; n=1; a moving branch ref, not a tag) for the per-pod
notifiers, the egress policy and the inhibit rule, against the lab's stand-in receiver: each section below says which rung it
reached and what was not exercised. Nothing here claims ANSSI compliance. The lab pass criteria are in `docs/monitoring-0.4.md`.

## The chain

    VMRule --vmalert--> Alertmanager --route--> receiver: chat webhook (slack)   [needs a real webhook]
                                      \--Watchdog--> receiver: heartbeat URL     [needs a real URL]

The default install has no external endpoint: the seed script writes placeholders, Alertmanager starts, alerts stay
visible in Alertmanager and Grafana, and the delivery to the outside fails by design. Always on, and
needing nothing external, are the rules that watch the chain itself (`vmrule-alerting.yaml`): VMAlert cannot reach
Alertmanager, rule evaluation errors, alert state not persisted, Alertmanager config not reloaded, Alertmanager absent.

## Both Alertmanager replicas receive every alert

Alertmanager runs two replicas that gossip (silences, the notification log), but an alert is held only by the replicas
vmalert sent it to. Through the headless Service vmalert resolved one pod at a time: in one sample (n=1, Scaleway, 3 CP + 6 W,
the 0.4.3 candidate, 2026-11-05) pod 0 held 5 alerts and pod 1 none (apps #207). So `notifiers` of `vmalert.yaml` now lists
one URL per Alertmanager pod (`http://vmalertmanager-alertmanager-<i>.vmalertmanager-alertmanager.services-observability.svc:9093`,
the pod FQDN the VM operator builds for its own discovery), and vmalert sends to all of them. `test-alert-delivery.py` fails
when the list and the `replicaCount` of the VMAlertmanager disagree: raise one, raise the other.

- `VMAlertNotifierErrors` (critical) now means every address fails; before, one pod being replaced would have paged "every alert
  is lost". `VMAlertNotifierPodErrors` (warning) means ONE address has failed for 30 minutes: the redundancy is gone. A pod that
  is down is also `AlertmanagerClusterDegraded`; a pod that is up but that vmalert cannot reach (its name does not resolve, a
  policy drops the sends) is only `VMAlertNotifierPodErrors`, because `AlertmanagerClusterDegraded` reads the scrape and the
  gossip, not what vmalert sends. A pod replaced in less than 30 minutes fires neither.
- Mocked/offline: the manifest, the rule on synthetic series, the operator and vmalert sources read. Read once on a cluster
  (lab 35 move 1; NOMINAL; n=1; a moving branch ref): both per-pod names resolved from a vmalert pod, and with `alertmanager-0`
  deleted the other pod answered at every 2-s sample for 41 s and nothing fired. The failing-send path was NOT exercised: the
  21 s the pod took to come back was shorter than vmalert's send cycle. To repeat the drill: delete the Alertmanager pod that
  holds an alert, read `/api/v2/alerts` on the other (it must show the alert), and read that the per-pod names resolve from a
  vmalert pod. Label the figures NOMINAL or INCIDENT with the cluster shape.

## Heartbeat (dead-man's-switch)

`Watchdog` fires forever. Alertmanager sends it about every 4 minutes (route `repeat_interval` 3m + `group_interval` 1m: a ping goes out at the first tick of the group interval after the repeat interval, so the gap is at most 4 min; with 5m it was measured at 360 s in lab 19) to the URL in `secret/observability/alertmanager-deadmansswitch`
(`OA_DEADMANSSWITCH_URL` of the seed script). The **service** pages you when the pings stop, which covers the cluster, the
node, vmalert or Alertmanager dying, and the egress being cut: the only signal that does not need the cluster. Set its grace
period above 5 minutes (15 is a floor). One URL, any ping-on-absence service.

## Which hosts Alertmanager may reach

The egress policy allows the chat host and the heartbeat host on 443, nothing else outside the cluster: `hooks.slack.com` and
`hc-ping.com` by default. A policy cannot read a Secret, so a different host is **one input each**, in the `cluster-identity`
ConfigMap (namespace `flux-system`):

| Key | Default | The host of |
|---|---|---|
| `ALERT_CHAT_HOST` | `hooks.slack.com` | the chat webhook (`alertmanager-slack`, `webhook-url`) |
| `ALERT_HEARTBEAT_HOST` | `hc-ping.com` | the dead-man's-switch URL (`alertmanager-deadmansswitch`, `url`) |

Set the key to the host name of the URL you seeded (no scheme, no path), then let Flux reconcile `observability-alert-egress`
(`flux reconcile kustomization observability-alert-egress`). Change the host and the secret URL **together**: a host that is
not the receiver's drops the notification on the wire, and a heartbeat host that does not match pages you for an outage that
is not happening. The policy lives in `apps/base/observability-alert-egress/` and is a Kustomization of its own on purpose:
Flux substitution applies to the whole render, and `observability` holds Grafana dashboards, VMRule templates and chart
values full of `$` expressions that it would blank. `test-alert-delivery.py` fails when the default hosts are not allowed,
when a host is written in full in the policy or in `vmalertmanager`, when the substitution is not optional, when
`observability` itself substitutes, or when a key is not documented here.

- Coming from 0.4.3: the `vmalertmanager` policy has no `toFQDNs` rule any more (it is `vmalertmanager-egress-hosts`). Remove any
  overlay patch of that rule, which 0.4.3 told you to make for another host: a patch by index can fail the build of
  `observability` or land on another rule. Set `ALERT_CHAT_HOST` and `ALERT_HEARTBEAT_HOST` instead.
- Mocked/offline: the manifests and the test. Read once on a cluster (lab 35 move 1; n=1): `vmalertmanager` had no `toFQDNs`
  rule left, `vmalertmanager-egress-hosts` existed and `observability-alert-egress` was Ready 3 s after `observability`. The
  largest gap between two Watchdog pings was 210 s, on the lab's stand-in receiver reached through a lab-only additive policy:
  the by-FQDN path to a real host and the gap of the swap were NOT measured. The policy is removed from `vmalertmanager` when
  `observability` applies the new revision and re-created by the new Kustomization once that dependency is satisfied (the
  dependency requeue is 30 s by default), so a notification may be retried once. After the move, read
  the largest gap between two Watchdog pings in the external service (4 minutes at most in nominal running, see Heartbeat) and
  that `alertmanager_notification_requests_failed_total` does not rise. Label the figures NOMINAL or INCIDENT with the cluster shape.
- Not done here: the keys are not written by the infrastructure repository from the tfvars yet. Until they are, set them with a
  patch of `cluster-identity` or of the `observability-alert-egress` Kustomization (`postBuild.substitute`) in your overlay.
  Nothing in the infrastructure repository removes a key you set by hand (a reading of its sources, not run): the Talos inline
  manifest creates the ConfigMap once and never updates it, and `cluster-identity-sync` merge-patches only the keys it renders.
  A cluster built from scratch gets neither key: set them after the install.

## Scheduled delivery test (opt-in)

Component `apps/flux/components/alert-delivery-probe`, off in every profile, for a cluster whose two secrets are real:

- `AlertDeliveryProbe` fires 10 minutes at 06:00 and 18:00 UTC and is routed (`probe="delivery"`) to a quiet chat receiver
  (one message, no "resolved").
- `AlertDeliveryFailing`: Alertmanager counted a failed notification attempt in the last 15 minutes (chat or heartbeat; it was one hour until lab 19 showed the alert lingering long after delivery was restored).
- `AlertDeliveryNotAcknowledged` (critical): the probe fired in the last 14 hours and no notification to the chat receiver
  succeeded in that time. "Acknowledged" means the receiver answered 2xx, which is what Alertmanager counts; a muted or
  unread channel behind a working webhook is not detected.
- While `AlertDeliveryNotAcknowledged` fires, Alertmanager **inhibits the Watchdog**: the heartbeat stops and the external
  service pages you through its own channel. So a broken chat leg is reported out of band without a second URL.
- A second inhibit rule mutes `CnpgWalArchiveFailing` while `CnpgWalNeverArchived` fires for the same Cluster (`equal: [cnpg_io_cluster]`): one fault, one critical page instead of two
  ten minutes apart (one observation, Scaleway 3 CP + 6 W, NOMINAL). Read once on a cluster (lab 35, n=1): the configuration was
  loaded with both inhibit rules, and a SYNTHETIC pair showed the second alert inhibited while the alert of another Cluster stayed
  active. The real WAL fault was not run against it, and the route grouping was not read.

Limits, stated: the failing-chat alert cannot reach the chat; an Alertmanager restart is covered because counters keep their
history in the window, but a lost counter series is not; the alert names above are tested on synthetic counters, the real
`alertmanager_notification_requests_*` series were read in lab 19 (one per attempt; the older `alertmanager_notifications_*` count a cycle at its start and its failure up to 5 min later, so they cannot say "no success").

## Routing

Watchdog first (it must never be swallowed), then the probe, then `severity="critical"` (hourly repeat), the rest to chat
(12 h repeat). `scripts/dev/test-alert-delivery.py` pins this order, the secrets each receiver reads, the inhibit rule,
and the egress hosts.
