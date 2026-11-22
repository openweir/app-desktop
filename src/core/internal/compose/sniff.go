package compose

// SniffFor sets the base section's TLS and HTTP sniffing for how the host takes traffic over
// (docs/routing.md §8.6). config is a configuration that starts with the base section: a
// composition, or a subscription of a format other than mihomo's (Normalize).
//
// With the TUN on and fake-ip DNS, every flow whose name an app resolved arrives with that
// name, from the fake-ip mapping. Overriding the destination with the sniffed name then only
// rewrites the flows an app sent to a bare IP, and those are the ones that dial a server by
// its IP with a borrowed name: REALITY, Hysteria 2 and TUIC clients, domain fronting. They
// would reach the site they disguise as instead. So there sniffing only feeds the rules.
// Without the TUN (the system proxy, or the mixed port alone) apps hand the proxy addresses
// they resolved themselves, possibly from poisoned DNS, and the node resolves the sniffed
// name instead, as the base section says.
func SniffFor(config map[string]any, tun bool) {
	if !tun || !fakeIP(config) {
		return
	}
	sniffer, _ := config["sniffer"].(map[string]any)
	sniff, _ := sniffer["sniff"].(map[string]any)
	for _, protocol := range []string{"HTTP", "TLS"} {
		if settings, ok := sniff[protocol].(map[string]any); ok {
			settings["override-destination"] = false
		}
	}
}

// fakeIP reports whether the configuration's DNS answers with fake IPs.
func fakeIP(config map[string]any) bool {
	dns, _ := config["dns"].(map[string]any)
	return dns["enable"] == true && dns["enhanced-mode"] == "fake-ip"
}
