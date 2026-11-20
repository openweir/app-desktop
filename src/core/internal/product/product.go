// Package product holds the identifiers that carry the product name, OpenWeir
// (docs/decisions.md D1). The names and signing identities come from product.json at the
// repository root, through product_gen.go (scripts/gen_product.py); src/openweir/product.py
// reads the same file.
package product

const (
	// SYSTEM and Administrators get full access; interactive users may read and write.
	PipeSDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)"

	ProtocolVersion = 1
)

// FrameMagic opens every frame on the Windows pipe.
var FrameMagic = [4]byte{'W', 'E', 'I', 'R'}

// Version is set at link time with -ldflags "-X <module>/internal/product.Version=<version>".
var Version = "dev"

// SignedRelease is set to "true" at link time, like Version, in the release workflow's
// builds, which it signs. The Windows service then accepts only a UI that WindowsSigner
// signed (docs/architecture.md §5.3). Unsigned builds, such as CI's and the test VM's, leave
// it off.
var SignedRelease = "false"
