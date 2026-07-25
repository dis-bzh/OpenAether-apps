# CCM Scaleway — volontairement HORS DAG Flux

Ce HelmRelease n'est référencé par **aucune** Kustomization Flux : l'ingress
public actuel passe par le pool **Cilium LB-IPAM** (`platform-cilium-lb-ipam`),
pas par un LoadBalancer managé provider. Le CCM est l'**autre** chemin
d'ingress (LB cloud Scaleway/OVH/Outscale → Service LoadBalancer), à câbler le
jour où on bascule : Flux Kustomization dédiée + Secret `scaleway-ccm-secret`
(via ESO) + retrait du pool LB-IPAM pour éviter deux chemins concurrents.
