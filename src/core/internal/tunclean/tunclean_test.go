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
