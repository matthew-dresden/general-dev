# Network module: exactly the network the remote engine needs, and nothing
# else. The gcp twin of provider/azure/modules/network, mirroring its
# structure and its comment discipline. See Section 5.6 of
# repos/spec/devcontainer-platform.md.
#
# Deliberately absent, by design rather than by omission: no external IP
# resource (gcp has none to attach -- the instance's interface simply
# declares no access_config, the compute module's doctrine note), no
# second firewall rule, and no VPN or interconnect of any kind. Ingress
# posture is ONE rule this module owns: tcp on the daemon's TLS port from
# the IAP range 35.235.240.0/20, targeted at the instance's network tag.
# That single rule is the whole inbound story -- the transport is the IAP
# tunnel (the transport_gcp module's wire contract), which terminates on
# the VM's nic0 through google's front end and is admitted by exactly this
# rule, so the daemon binds 0.0.0.0 behind a network whose only door is
# this guard.
#
# A custom-mode VPC (auto_create_subnetworks = false) carries no implied
# allow rules at all -- unlike an auto-mode network or azure's built-in
# defaults -- so before this module's one rule exists, the network admits
# nothing from anywhere: the posture fails closed while resources converge,
# which is why no ordering edge toward the instance is required (the
# compute module's header records the same reasoning from the other side).
# The VPC's default route (0.0.0.0/0 to the default internet gateway,
# created with every network) is the outbound path's other half and is
# kept: the instance has no external address, so that route carries
# nothing by itself -- Cloud NAT below is what gives it a next hop the
# instance can actually use.
#
# The Cloud Router and Cloud NAT are this module's because egress is a
# network property: an internal-only instance reaches the package
# archives, the container registry and google's APIs only through a NAT
# gateway, and gcp has no default-outbound default to inherit (the azure
# twin pins default_outbound_access_enabled for the same reason). Auto
# NAT address allocation keeps the composition free of a statically
# reserved address to manage; ALL_SUBNETWORKS_ALL_IP_RANGES covers every
# subnetwork of this VPC, so a deployment reusing this network's other
# subnetworks egresses through the same gateway.

resource "google_compute_network" "this" {
  name    = "${var.name_prefix}-vnet"
  project = var.project_id

  # Custom subnetwork mode is the whole point of this resource: no
  # auto-created subnetwork in every region, nothing but the subnetwork
  # this module itself declares. Every implicit default an auto-mode
  # network would carry (a subnetwork per region with pre-set CIDRs) is a
  # shape this composition refuses to inherit unexamined.
  auto_create_subnetworks = false

  # The default route (0.0.0.0/0 -> default-internet-gateway) is created
  # with the network and kept: see the header comment. Deleting it here
  # would strand the Cloud NAT path below, which routes out through the
  # gateway route's next hop.
  delete_default_routes_on_create = false
}

resource "google_compute_subnetwork" "this" {
  name          = "${var.name_prefix}-subnet"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.this.id
  ip_cidr_range = var.subnet_cidr

  # Private Google Access stays off by explicit choice, recorded rather
  # than defaulted: with it on, google-API traffic (the Secret Manager
  # REST reads the startup script makes) would route privately; with it
  # off -- the default this declaration pins -- that traffic egresses
  # through Cloud NAT like every other outbound flow. One egress path is
  # one thing to reason about; enabling PGA later is a one-line,
  # reviewed change, not a hidden default.
  private_ip_google_access = false
}

resource "google_compute_router" "this" {
  name    = "${var.name_prefix}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name    = "${var.name_prefix}-nat"
  project = var.project_id
  region  = var.region
  router  = google_compute_router.this.name

  # Addresses are allocated automatically and managed by google: the
  # composition holds no static IP to reserve, track and pay for while
  # idle.
  nat_ip_allocate_option = "AUTO_ONLY"

  # Every subnetwork of this VPC, all IP ranges: the instance's subnetwork
  # egresses through this gateway, and any subnetwork later added to the
  # same VPC inherits the same path without a second resource.
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  # The connection-level defaults are the stated values, not surprises:
  # no endpoint-independent mapping (the daemon's traffic is
  # outbound-initiated TLS; nothing behind the NAT expects unsolicited
  # inbound), fixed port allocation with google's default per-VM port
  # reservation -- the idle timeouts keep the provider's own defaults,
  # which are documented values, not decisions this composition makes.
  enable_endpoint_independent_mapping = false
  enable_dynamic_port_allocation      = false
  min_ports_per_vm                    = 64
}

# The one ingress rule in the whole composition: tcp on the daemon's TLS
# port, from the IAP range, targeted at the instance's network tag. The
# range is google's documented IAP egress range -- the addresses google's
# Identity-Aware Proxy front end dials the VM from -- and the tunnel that
# terminates there is authenticated by google's IAP before it ever reaches
# the network, so the rule admits google's front end and nothing else.
resource "google_compute_firewall" "iap_ingress" {
  name          = "${var.name_prefix}-iap-ingress"
  project       = var.project_id
  network       = google_compute_network.this.name
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]

  allow {
    protocol = "tcp"
    ports    = [var.docker_tls_listen_port]
  }

  # The instance tag the root composition composes from the same
  # name_prefix the VM's tags list carries: the rule targets exactly the
  # instances this composition creates and no other, and a VM without the
  # tag is unreachable even from IAP.
  target_tags = [var.instance_tag]

  # Flow logging is off by omission -- no log_config block is declared,
  # which is how google_compute_firewall expresses "logging disabled": the
  # daemon's TLS listener is the audited surface, not the VPC's packet
  # stream, and the one rule's admits/denies are visible in the daemon's
  # own mTLS handshake failures. The absence IS the doctrine, the same
  # shape the interface's missing access_config is: there is no flag left
  # at false to flip back.
}
