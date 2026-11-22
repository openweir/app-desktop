package compose

import (
	"context"
	"errors"
	"fmt"
	"maps"

	"github.com/metacubex/mihomo/common/yaml"
	"github.com/openweir/subconv"
	"github.com/openweir/subconv/rules"
)

// Normalized is a subscription as the core reads it: an OpenWeir mihomo profile, whatever the
// format it came in (Normalize).
type Normalized struct {
	// Profile is the profile as a mapping. Its proxies, proxy providers, groups, rule
	// providers and rules are subconv's, each item one mihomo loads.
	Profile map[string]any
	Format  subconv.Format
	// Warnings are what the conversion left out or changed: a broken node, a rule mihomo
	// would refuse, a renamed duplicate. The rest of the subscription is read regardless.
	Warnings []subconv.Warning
	// Stats count the subscription's own rules and how many survived.
	Stats subconv.RuleStats
	// Template says that subconv's routing template took the place of the subscription's
	// groups, rule providers and rules, and why; nil when they are the subscription's own.
	Template *subconv.TemplateUse
	// Rules are a mihomo or Clash profile's rules as written, nil for the other formats and
	// with the template: ConvertToPlan reads them itself, to say why it leaves each one out.
	Rules any
	// Base says that the profile starts with the base section (a format other than mihomo's),
	// whose sniffing follows the host's TUN (SniffFor).
	Base bool
}

// HasRules reports whether the subscription routes: by rules of its own that survived, not
// only the MATCH subconv gives a profile of groups without rules, or by the template.
func (n *Normalized) HasRules() bool {
	rules, _ := n.Profile["rules"].([]any)
	return len(rules) > 0 && (n.Stats.Total > 0 || n.Template != nil)
}

// The parts of a profile subconv converts; the rest of a mihomo profile (ports, DNS,
// sniffing...) stays the subscription's own. Clash's early spellings of them go too.
var (
	convertedParts = []string{"proxies", "proxy-providers", "proxy-groups", "rule-providers", "rules"}
	legacyParts    = []string{"Proxy", "Proxy Group", "Rule"}
	// A mapping with one of these routes without proxies of its own, which subconv does not
	// take for a subscription: a mihomo profile that sends everything DIRECT, say.
	routingParts = []string{"proxy-groups", "rule-providers", "rules"}
)

// Normalize reads a subscription of any format github.com/openweir/subconv reads (mihomo,
// Clash and Stash YAML, sing-box and Xray JSON, share links plain or base64, Surge, Loon,
// Shadowrocket, Quantumult X, SIP008, ssd://) into an OpenWeir mihomo profile (D54).
//
// subconv checks every item on its own: a node, group, rule or provider mihomo would refuse,
// which would fail the whole profile, is repaired or left out with a warning, and the rest
// is read. A mihomo, Clash or Stash profile keeps its other settings; any other format gets
// the base section (docs/routing.md §8.6) in front of its converted parts.
//
// With a template client (routingtemplate.Source.Client), a subscription with no rules, whose
// final rule did not survive or that lost more than a tenth of its rules routes by subconv's
// routing template instead (subconv.Build, RulesAuto); the client reads a cache, so reading
// never waits for the network. Without one the subscription keeps its own routing.
//
// The proxy pool (without a template: it takes only the nodes), InspectSource, ConvertToPlan
// and a subscription's own rules (the admission pipeline) all read subscriptions through it.
func Normalize(data []byte, template *rules.Client) (*Normalized, error) {
	result, err := convert(data, template)
	var original map[string]any
	switch {
	case err == nil:
		if result.Format == subconv.FormatClash || result.Format == subconv.FormatStash {
			original = mapping(data)
		}
	default:
		original = routingOnly(data, err)
		if original == nil {
			return nil, readError(err)
		}
		withProxies := maps.Clone(original)
		withProxies["proxies"] = []any{}
		encoded, marshalErr := yaml.Marshal(withProxies)
		if marshalErr != nil {
			return nil, marshalErr
		}
		if result, err = convert(encoded, template); err != nil {
			return nil, readError(err)
		}
	}
	if len(result.Proxies) == 0 && len(result.ProxyProviders) == 0 && len(result.Groups) == 0 &&
		len(result.Rules) == 0 && len(result.Warnings) > 0 {
		// Every item was left out: say why the first was.
		first := result.Warnings[0]
		return nil, fmt.Errorf("nothing in the subscription could be read: %s: %s", first.Code, first.Detail)
	}
	parts, err := result.YAML()
	if err != nil {
		return nil, err
	}
	var fragment map[string]any
	if err := yaml.Unmarshal(parts, &fragment); err != nil {
		return nil, fmt.Errorf("the converted subscription: %w", err)
	}
	profile := original
	if profile == nil {
		if err := yaml.Unmarshal(baseYAML, &profile); err != nil {
			return nil, fmt.Errorf("base section: %w", err)
		}
	} else {
		profile = maps.Clone(profile)
		for _, keys := range [][]string{convertedParts, legacyParts} {
			for _, key := range keys {
				delete(profile, key)
			}
		}
	}
	maps.Copy(profile, fragment)
	if _, ok := profile["proxies"]; !ok {
		profile["proxies"] = []any{}
	}
	out := &Normalized{
		Profile: profile, Format: result.Format, Warnings: result.Warnings, Stats: result.RuleStats,
		Template: result.Template, Base: original == nil,
	}
	if original != nil && result.Template == nil {
		out.Rules = original["rules"]
	}
	return out, nil
}

// convert runs subconv, with the routing template when there is a client for it.
func convert(data []byte, template *rules.Client) (*subconv.Result, error) {
	if template == nil {
		return subconv.Convert(data)
	}
	return subconv.Build(context.Background(), data, subconv.Options{Rules: subconv.RulesAuto, Client: template})
}

// mapping decodes a YAML mapping as mihomo does, anchors and merge keys resolved; nil when
// the data is not one, such as a profile subconv read from base64 or from broken YAML.
func mapping(data []byte) map[string]any {
	var doc map[string]any
	if yaml.Unmarshal(data, &doc) != nil {
		return nil
	}
	return doc
}

// routingOnly is a mihomo profile with groups, rule providers or rules and no proxies, which
// subconv refuses (err) for want of nodes; nil for anything else, which is no subscription.
func routingOnly(data []byte, err error) map[string]any {
	if errors.Is(err, subconv.ErrEmpty) || errors.Is(err, subconv.ErrWebPage) {
		return nil
	}
	doc := mapping(data)
	for _, key := range routingParts {
		if _, ok := doc[key]; ok {
			return doc
		}
	}
	return nil
}

// readError says why a body is no subscription.
func readError(err error) error {
	switch {
	case errors.Is(err, subconv.ErrEmpty):
		return errors.New("the subscription is empty")
	case errors.Is(err, subconv.ErrWebPage), errors.Is(err, subconv.ErrMessage):
		// subconv's text says which, with the server's message.
		return err
	case errors.Is(err, subconv.ErrUnknownFormat):
		return errors.New("neither a mihomo profile nor share links, nor another subscription format the core reads")
	}
	return fmt.Errorf("the subscription: %w", err)
}
