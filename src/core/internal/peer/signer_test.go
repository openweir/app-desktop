package peer

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"math/big"
	"slices"
	"strings"
	"testing"
	"time"
)

// certificate is a self-signed DER certificate for commonName with the given extended key
// usages; none leaves out the extension.
func certificate(t *testing.T, commonName string, usages ...asn1.ObjectIdentifier) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:       big.NewInt(1),
		Subject:            pkix.Name{CommonName: commonName, Organization: []string{commonName}},
		NotBefore:          time.Now().Add(-time.Hour),
		NotAfter:           time.Now().Add(72 * time.Hour),
		KeyUsage:           x509.KeyUsageDigitalSignature,
		UnknownExtKeyUsage: usages,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return der
}

func identifier(t *testing.T, dotted string) asn1.ObjectIdentifier {
	t.Helper()
	var parsed asn1.ObjectIdentifier
	for part := range strings.SplitSeq(dotted, ".") {
		value, ok := new(big.Int).SetString(part, 10)
		if !ok || !value.IsInt64() {
			t.Fatalf("not an object identifier: %s", dotted)
		}
		parsed = append(parsed, int(value.Int64()))
	}
	return parsed
}

var codeSigning = asn1.ObjectIdentifier{1, 3, 6, 1, 5, 5, 7, 3, 3}

// A release's signer, as a signed build links into product.WindowsSigner and WindowsSignerEKU.
const (
	testSigner    = "OpenWeir"
	testSignerEKU = "1.3.6.1.4.1.311.97.0"
)

func TestSignerNeedsTheNameAndTheProfilesUsage(t *testing.T) {
	release := Signer{Name: testSigner, EKU: testSignerEKU}
	profile := identifier(t, testSignerEKU)
	// As Artifact Signing issues them: code signing and the certificate profile's own usage.
	ours := certificate(t, testSigner, codeSigning, profile)
	if err := release.check(testSigner, ours); err != nil {
		t.Fatalf("OpenWeir's certificate: %v", err)
	}
	// Another organization verified under the same name gets its own profile's usage.
	namesake := certificate(t, testSigner, codeSigning, identifier(t, "1.3.6.1.4.1.311.97.1.2.3.4"))
	if err := release.check(testSigner, namesake); err == nil || !strings.Contains(err.Error(), "without the extended key usage") {
		t.Fatalf("a namesake's certificate: %v", err)
	}
	if err := release.check(testSigner, certificate(t, testSigner)); err == nil {
		t.Fatal("accepted a certificate without extended key usages")
	}
	if err := release.check("Someone Else", ours); err == nil || !strings.Contains(err.Error(), `not "OpenWeir"`) {
		t.Fatalf("another signer: %v", err)
	}
	if err := release.check(testSigner, []byte("not a certificate")); err == nil {
		t.Fatal("accepted what is not a certificate")
	}
}

func TestExtendedKeyUsages(t *testing.T) {
	usages, err := extendedKeyUsages(certificate(t, "OpenWeir", codeSigning, identifier(t, testSignerEKU)))
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"1.3.6.1.5.5.7.3.3", testSignerEKU}; !slices.Equal(usages, want) {
		t.Fatalf("usages = %v, want %v", usages, want)
	}
	if usages, err := extendedKeyUsages(certificate(t, "OpenWeir")); err != nil || usages != nil {
		t.Fatalf("without the extension: %v, %v", usages, err)
	}
}
