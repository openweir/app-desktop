//go:build with_naive

package engine

import (
	"testing"

	C "github.com/metacubex/mihomo/constant"
)

func TestForkNaiveNativeRuntimeAndAdmission(t *testing.T) {
	for _, composed := range []bool{false, true} {
		for _, quic := range []bool{false, true} {
			node := map[string]any{
				"name": "node", "type": "naive", "server": "127.0.0.1", "port": 443,
				"username": "test", "password": "test", "quic": quic, "udp-over-tcp": true,
			}
			admitted, err := admitProfile(forkProfile(t, node, composed), noTUN)
			if err != nil {
				t.Fatal(err)
			} // purego also binds the actual DLL here
			proxy := forkNode(admitted.cfg)
			if proxy == nil || proxy.Type() != C.Naive || !proxy.SupportUDP() {
				t.Fatal("Naive/UoT capabilities were not retained")
			}
			if _, ok := tcpNode(proxy); ok {
				t.Fatal("optional QUIC cannot be treated as always-TCP")
			}
			_ = proxy.Close()
			// A subscription's Naive node without UoT reaches the fork through subconv (D54),
			// which drops its UDP; the fork itself refuses one that an override brings in.
			node["udp-over-tcp"], node["udp"] = false, true
			admitted, err = admitProfile(forkProfile(t, node, composed), noTUN)
			if err != nil {
				t.Fatal(err)
			}
			proxy = forkNode(admitted.cfg)
			if proxy == nil || proxy.SupportUDP() {
				t.Fatal("UDP without UoT was kept")
			}
			_ = proxy.Close()
			if !forkRefuses(t, node, composed) {
				t.Fatal("accepted UDP without UoT")
			}
		}
	}
}
