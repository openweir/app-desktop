package tunclean

import (
	"testing"

	"github.com/openweir/app-desktop/src/core/internal/product"
)

func TestTheProductPinsAreValid(t *testing.T) {
	pins := Pins{Device: product.LinuxTunDevice, Table: product.LinuxRouteTable, RuleIndex: product.LinuxRuleIndex}
	if err := pins.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestPinsRefuseTheKernelsTablesAndRules(t *testing.T) {
	valid := Pins{Device: "openweir0", Table: 9347, RuleIndex: 9347}
	for name, pins := range map[string]Pins{
		"no device":       {Table: 9347, RuleIndex: 9347},
		"long device":     {Device: "openweir0123456789", Table: 9347, RuleIndex: 9347},
		"random table":    {Device: "openweir0", RuleIndex: 9347},
		"main table":      {Device: "openweir0", Table: 254, RuleIndex: 9347},
		"local rule":      {Device: "openweir0", Table: 9347},
		"rules over main": {Device: "openweir0", Table: 9347, RuleIndex: 32760},
		"negative rule":   {Device: "openweir0", Table: 9347, RuleIndex: -1},
	} {
		if pins.Validate() == nil {
			t.Errorf("%s: %+v passed", name, pins)
		}
	}
	if err := valid.Validate(); err != nil {
		t.Fatal(err)
	}
}

// The kernel refuses these names or, for %d, picks a name of its own; resolvectl would read
// a leading dash as an option.
func TestPinsRefuseWhatIsNoInterfaceName(t *testing.T) {
	for _, device := range []string{".", "..", "tun/0", "tun:0", "tun 0", "tun\t0", "tun%d", "-h", "--help"} {
		if (Pins{Device: device, Table: 9347, RuleIndex: 9347}).Validate() == nil {
			t.Errorf("device %q passed", device)
		}
	}
	for _, device := range []string{"openweir0", "a", "tun-0", "tun_0", "tun.0", "fifteen-bytes-x"} {
		if err := (Pins{Device: device, Table: 9347, RuleIndex: 9347}).Validate(); err != nil {
			t.Errorf("device %q: %v", device, err)
		}
	}
}

// sing-tun writes its rules from the index to ten above it, the last its exclusions jump to,
// and removes that range itself (tun_linux.go, unsetRules).
func TestPinsHoldTheElevenRulesFromTheIndex(t *testing.T) {
	pins := Pins{Device: "openweir0", Table: 9347, RuleIndex: 9347}
	held := 0
	for priority := 0; priority <= 32767; priority++ {
		if pins.HoldsRule(priority) {
			held++
		}
	}
	if held != RuleSpan+1 {
		t.Fatalf("the pins hold %d priorities, want %d", held, RuleSpan+1)
	}
	for priority, want := range map[int]bool{9346: false, 9347: true, 9352: true, 9357: true, 9358: false, 0: false, 32766: false} {
		if pins.HoldsRule(priority) != want {
			t.Errorf("HoldsRule(%d) = %v", priority, !want)
		}
	}
	// The last valid index keeps its last rule before main's.
	last := Pins{Device: "openweir0", Table: 9347, RuleIndex: 32766 - RuleSpan - 1}
	if err := last.Validate(); err != nil || last.HoldsRule(32766) || !last.HoldsRule(32765) {
		t.Fatalf("%+v: %v", last, err)
	}
	if (Pins{Device: "openweir0", Table: 9347, RuleIndex: 32766 - RuleSpan}).Validate() == nil {
		t.Fatal("pins whose last rule is main's passed")
	}
}
