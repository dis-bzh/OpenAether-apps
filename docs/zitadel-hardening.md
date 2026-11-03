# Zitadel and Grafana authentication: what is set, where it applies, what an existing cluster needs

> Addresses ANSSI PA-079 (R10, R12, R21, R23, R29), PG-078 and PA-012 R27 in part. Rung: the Zitadel settings were run
> through a real Zitadel (v4.19.5, the image tag the HelmRelease sets; the chart's own appVersion is v4.19.2) against PostgreSQL by `scripts/dev/test-zitadel-config.sh`, and
> Grafana's through a real Grafana 11.3.1 (the chart now ships 13.2.4, which was started with the shipped settings and dashboards;
> the login behaviours below were not re-run on it); nothing was run on a cluster, with OpenBao, External Secrets or Istio.

## The one thing to know first

Zitadel reads `FirstInstance` and `DefaultInstance` **once, when the instance is created** (the setup Job on an empty
database). On a cluster whose Zitadel is already initialised they change **nothing**: the test proves it (a running instance
keeps its 8-character policy and open registration after the new values are rolled out). So:

| Setting | Where | New cluster | Existing cluster |
|---|---|---|---|
| First admin password from OpenBao, change forced | `FirstInstance` + `zitadel-secret-config` | applied | **not applied**: `zitadel-admin` may still have `Password1!` |
| 15-character policy, lockout at 5, MFA forced, no self-registration, no external IdP, 12 h check lifetimes, 1 h tokens | `DefaultInstance.*` | applied | **not applied**: `scripts/zitadel-apply-policies.py` |
| argon2id password hashing | `SystemDefaults.PasswordHasher` | applied | applied at the next rollout, existing hashes kept (below) |
| JSON logs and the HTTP access log on stdout | `Log`, `LogStore` | applied | applied at the next rollout |
| Grafana sessions 2 h idle / 12 h, request log as JSON | `grafana.yaml` | applied | applied at the next rollout |
| Grafana SSO-only (no local form, strict roles) | `apps/base/observability-sso-only` | **off** | **off**: you choose |

**A cluster rebuilt by the disaster-recovery chain has no `iam-admin` and no `iam-admin-pat`** (0.4.1, observed in a proof lab).
Every command below that reads `iam-admin-pat`, and the break-glass, need a token that such a cluster does not have: see the end of
"Locked out, and the break-glass".

## A new cluster

`configure.sh` creates `secret/zitadel/admin` once (`{username: zitadel-admin, password}`: 28 random characters and the
suffix `Aa1!`, which guarantees the four character classes Zitadel's policy requires). External Secrets puts the password
into `zitadel-secret-config`, the setup Job reads it. Read it **once**, with the `bao_admin` helper of the vault README:

```bash
bao_admin kv get -mount=secret -field=password zitadel/admin
```

Log in at `https://zitadel.openaether.local` as `zitadel-admin@zitadel.zitadel.openaether.local`. The user is created with
a forced password change (checked in the test), and the login policy forces MFA, so expect to be asked for a second factor
too (an authenticator app or a security key; no SMS provider is configured). That second part is Zitadel's documented
behaviour and was not clicked through. **The OpenBao value is the initial password, not the current one**: it goes stale at
that first login, by design.

## An existing cluster

1. **Is `Password1!` still live?** Ask Zitadel (a session check; it creates nothing you need to clean). On a rebuilt cluster the
   `iam-admin-pat` Secret read here does not exist: use the PAT of a machine user you created (end of "Locked out, and the break-glass"):
   ```bash
   kubectl -n services-identity port-forward svc/zitadel 8080:8080 &   # or use https://zitadel.openaether.local
   PAT="$(kubectl -n services-identity get secret iam-admin-pat -o jsonpath='{.data.pat}' | base64 -d)"
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/v2/sessions \
     -H "Host: zitadel.openaether.local" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
     -d '{"checks":{"user":{"loginName":"zitadel-admin@zitadel.zitadel.openaether.local"},"password":{"password":"Password1!"}}}'
   ```
   `201` means the public default is live: change the password in the console now. `400` means it is not.
2. **The key `zitadel/admin`.** Flux recreates the `openbao-vault-bootstrap` Job on every sync (`force`), so the new
   `configure.sh` seeds it (create only) when this revision lands. Until it exists the `zitadel-secret-config`
   ExternalSecret reports `SecretSyncedError`; it is expected to keep serving the previous Secret, so Zitadel is
   untouched. The value is never used by an initialised instance.
3. **The policies**, from the same values as the HelmRelease, so the two cannot disagree. Dry run first:
   ```bash
   ZITADEL_PAT="$PAT" python3 scripts/zitadel-apply-policies.py --url http://127.0.0.1:8080 --host zitadel.openaether.local
   ZITADEL_PAT="$PAT" python3 scripts/zitadel-apply-policies.py --url http://127.0.0.1:8080 --host zitadel.openaether.local --apply
   ```
   Before `--apply`: **5 wrong passwords lock an account** (it also locks a human who forgot theirs), and **MFA becomes mandatory**
   for every human at their next login (they enrol then). The machine PAT keeps working after the policy is applied (the test
   unlocks an account with it afterwards). Org-level policies created in the
   console override these instance ones and are not touched.
4. **Hashing.** At the next rollout Zitadel hashes with argon2id (32 MiB, 3 passes, 4 threads: Zitadel's own defaults) and
   still verifies bcrypt through `Verifiers`, rewriting each hash at that user's next successful login. The test shows both
   halves: **without** the `bcrypt` verifier nobody can log in (HTTP 500 on the password check), with it the old password works
   and is re-hashed. Each concurrent hash holds 32 MiB, against the 1 Gi limit: raise `Memory` only with the limit.

## Locked out, and the break-glass

`iam-admin` is the machine user the chart creates at installation (IAM_OWNER, a key in `iam-admin` and a PAT in
`iam-admin-pat`, **both valid to 2029-01-01**: that is the chart's default, not changed here, because it is also the way back in
when the only human admin is locked). Unlock a human:

```bash
UID_=$(curl -s -X POST http://127.0.0.1:8080/v2/users -H "Host: zitadel.openaether.local" -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"filters":[{"userNameFilter":{"userName":"zitadel-admin","method":"TEXT_FILTER_METHOD_EQUALS"}}]}' | jq -r '.result[0].userId')
curl -s -X POST "http://127.0.0.1:8080/v2/users/$UID_/unlock" -H "Host: zitadel.openaether.local" \
  -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' -d '{}'
```

Treat the PAT as a root credential: it is a standing IAM_OWNER secret in the cluster for as long as the Secret exists
(deleting `iam-admin-pat` after bootstrap is possible, and removes this way back).

### After a disaster-recovery rebuild: this break-glass is gone

On a cluster rebuilt by the DR chain (`task dr-restore-plan`, OpenAether-infra) the two Secrets above do not exist. Observed in a
proof lab on Scaleway (3 CP + 6 W, after a rebuild by the chain): `kubectl -n services-identity get secret iam-admin-pat` answered
`not found`, and `kubectl get secrets -A` listed no Secret with `iam-admin` in its name. The cause is a reading, not a separate
experiment: the machine user and its PAT come from `FirstInstance`, which Zitadel reads once, when the instance is created ("The
one thing to know first"). The rebuild restores the Zitadel database, so the instance already exists and the setup Job creates
neither Secret; they are Kubernetes Secrets written by the chart's setup Job and nothing in this repository stores them in OpenBao.
Whether the machine user itself is still in the restored database was not checked.

- **The identity data is intact.** The restored `zitadel` database holds the original instance and its users: in the lab it held
  the user created before the disaster, with its password and second-factor events, and instance events from the first install up
  to the setup Job of the rebuild (read in SQL). A sign-in of that user after the rebuild was **not** shown: the lab's scripted
  way needs the PAT, and no browser run was made.
- **What exists instead:** the humans of that database with the passwords and second factors they set before the disaster
  (`secret/zitadel/admin` in OpenBao is the initial password of `zitadel-admin` only, stale after its first login). A human
  IAM_OWNER who can still sign in can create a machine user and a personal access token in the console and use that token in
  place of `$PAT` above (Zitadel's console feature; not run on a rebuilt cluster).
- **What does not exist:** a token you hold for a machine user, so the scripted path (`scripts/zitadel-apply-policies.py`, the unlock
  above) has nothing to authenticate with, and **no way back in if no human admin can sign in** (locked at 5 wrong passwords, a
  lost second factor). No recovery for that case is documented or tested in 0.4.1, and `FirstInstance` cannot be run again on an
  existing instance. Its design is for a later release. Saving the PAT outside the cluster before a disaster is the obvious
  mitigation; whether a saved PAT works against the restored database was not tried.

## Grafana

The base brick now signs users out after 2 h idle or 12 h in all (the defaults were 7 and 30 days) and logs every HTTP
request as a JSON line, which is the only access trail Grafana OSS offers (Alloy ships it to Loki). It still has the local
login form and the permissive mapping (every Zitadel account becomes a Viewer, and a Viewer can query Loki, OpenBao's audit
log included): changing who can sign in is a decision, so it is an overlay, **off**.

To turn it on (`apps/base/observability-sso-only`):

1. In Zitadel, in the project Grafana uses, create the role `grafana-viewer` next to `grafana-admin`, and **grant it to
   everyone who reads logs today**. Everyone without a role is locked out afterwards.
2. Prove an SSO login first (a `grafana-admin` account reaches Grafana through the Zitadel button).
3. Point the Flux Kustomization `observability` (`apps/flux/base`) at `./apps/base/observability-sso-only`.
   The local form disappears; the admin from the `grafana-admin` Secret still works by HTTP basic auth against the API
   (verified on Grafana 11.3.1: `disable_login_form` hides the form, it does not disable the account) and is the break-glass.
4. Undo = point the path back. Nothing else differs between the two (`scripts/dev/test-grafana-auth.sh`).

With Zitadel's self-registration off (new clusters, or step 3 of the previous section), the Viewer fallback is much less open
than it was; it is still every account of the instance.

## Zitadel v4.19.5 (0.3.2): what changed for an operator

v4.19.5 fixes three HIGH advisories that allow account takeover (GHSA-mchc-246r-6m76 header injection, GHSA-84fg-x5h4-w8xv
forgeable email verification proof in the Login V2, GHSA-qj3q-235g-qr6w device code). Affected: 4.0.0 to 4.19.4. The
HelmRelease overrides `image.tag` and `login.image.tag` (tag and digest) because the chart 10.4.0 still carries v4.19.2.

- **Session cookie secret.** The login UI signs its session cookie with `ZITADEL_SESSION_COOKIE_SECRET`; v4.19.5 asks that it
  is random and not an example value. `login.sessionCookieSecretName` now points at the ExternalSecret
  `zitadel-login-session-cookie`, fed by `secret/zitadel/session-cookie` (48 random characters, created by `configure.sh`,
  create only). Effect on a running cluster: the login signs with a new key, so open login sessions end once and users sign in
  again. To rotate, set the OpenBao value to `new,old` (the first entry signs, all verify), then drop `old` later.
  Until the key exists in OpenBao the new login pods stay in `CreateContainerConfigError` and the old ones keep serving.
- **List endpoints enforce query limits.** Nothing in this repository lists objects: `scripts/zitadel-apply-policies.py` reads
  and writes single policies, Grafana only uses the OIDC endpoints, and the setup Job is the chart's. A script of yours that
  pages through `_search` or `/v2/.../list` calls must send an explicit `limit`.
- **The device authorization flow has a consent screen.** A user who starts a device login now confirms it. No client of this
  platform uses the device flow; if you register one, tell your users to expect the screen.
- **Not covered by this bump:** the login's `next` is 16.3.6, which newer advisories flag again (fixed in 16.4.1); they are
  waived with an expiry in `.vex/openaether-apps.openvex.json` until Zitadel ships a newer `next`.
- **The login page is routed through the Gateway** (apps #161): `/ui/v2/login` goes to `zitadel-login:3000`, and the Gateway may
  reach that pod on port 3000 and nothing else of it. Before, the authorize step of any OIDC client ended on a 404 for a person in
  a browser. The image optimizer of the login, `/ui/v2/login/_next/image`, is sent to the server and answers 404 on purpose (the
  VEX statements about Next.js rest on it); `scripts/dev/test-zitadel-login-route.py` keeps both rules. Proven by manifests only:
  a browser sign-in with a second factor into Grafana is a lab check. A cluster without public DNS for the Zitadel host, or whose
  Grafana does not trust the Gateway's CA, cannot complete the token exchange: Grafana's egress allows `zitadel:8080` only.

## What this does not do

- **No password expiry for admins** (ANSSI PA-079 R25, 1 to 3 years): `PasswordAgePolicy` is not set and whether Zitadel
  enforces it was not checked. No breached-password check (R27): Zitadel has none.
- **Authentication events are not in Loki.** Logins, failures, role grants and federation events live in Zitadel's eventstore.
  What is in Loki now is the HTTP access log (path, status, client headers, with Authorization and cookies redacted by Zitadel;
  the URL keeps its query string, so Alloy blanks `id_token_hint`, `*_token`, `*_secret` and `assertion` values before Loki,
  `scripts/dev/test-alloy-scrub.sh`; other secrets in a URL are not known to it)
  and the runtime log as JSON: it tells that `/oauth/v2/authorize` was hit, not by whom. Exporting events needs a service user
  with a standing token and a small exporter; not built (PA-012 Annex A, R29).
- **Loki is still one tenant:** a Grafana Viewer reads every namespace's logs. A separate tenant for the OpenBao audit trail
  changes the log data model (Alloy, the datasource, the existing data) and is not a default-off change.
- **The admin UIs are still reachable through the shared Gateway** when the application load balancer is public (no ACL,
  no AuthorizationPolicy by source): only the first-admin default is closed here. Removing the Vault and Grafana routes from
  the public path would take the Grafana UI away from every operator who uses it that way today; that is an exposure
  decision, together with the load-balancer ACL on the infrastructure side.
- **Grafana keeps PBKDF2 at 10,000 iterations for its local admin** (upstream's fixed cost). ANSSI R29 asks for a memory-hard
  function for stored passwords; Zitadel now has one, Grafana's local account cannot. SSO-only hides it, nothing more.
- **`iam-admin`'s key and PAT stay valid to 2029**, see above, on a cluster that was not rebuilt: after a DR rebuild they do not exist.
- Zitadel Actions, SMTP (so no email for password reset) and the Zitadel login v2 UI texts were not looked at.
