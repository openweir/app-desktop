package compose

import (
	"fmt"

	"github.com/metacubex/mihomo/common/yaml"
	"github.com/openweir/subconv"
)

// Normalize returns a subscription in the form the core reads: a sing-box config becomes an
// OpenWeir mihomo profile (github.com/openweir/subconv), the base section with the converted
// proxies, groups, rule providers and rules (docs/routing.md §8.6). mihomo profiles and share
// links, which mihomo reads itself, come back unchanged.
//
// The proxy pool, InspectSource, ConvertToPlan and a subscription's own rules (the admission
// pipeline) all read subscriptions through it, so a sing-box subscription works wherever a
// mihomo one does.
func Normalize(data []byte) ([]byte, error) {
	if format, err := subconv.Detect(data); err != nil || format != subconv.FormatSingBox {
		return data, nil
	}
	converted, err := subconv.Convert(data)
	if err != nil {
		return nil, fmt.Errorf("sing-box subscription: %w", err)
	}
	parts, err := converted.YAML()
	if err != nil {
		return nil, fmt.Errorf("sing-box subscription: %w", err)
	}
	var profile, fragment map[string]any
	if err := yaml.Unmarshal(baseYAML, &profile); err != nil {
		return nil, fmt.Errorf("base section: %w", err)
	}
	if err := yaml.Unmarshal(parts, &fragment); err != nil {
		return nil, fmt.Errorf("sing-box subscription: %w", err)
	}
	for key, value := range fragment {
		profile[key] = value
	}
	if _, ok := profile["proxies"]; !ok {
		profile["proxies"] = []any{}
	}
	return yaml.Marshal(profile)
}
