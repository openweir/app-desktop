package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
)

// The tests sign with a key pair of their own, made afresh each run.
func testKey(t *testing.T) (string, ed25519.PrivateKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(public), private
}

func write(t *testing.T, path string, data []byte) string {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// release is a release's three files as the UI downloads them, into a directory of the user's.
type release struct {
	pkg, sums, signature string
}

func signedRelease(t *testing.T, private ed25519.PrivateKey, name string, body []byte) release {
	t.Helper()
	dir := t.TempDir()
	digest := sha256.Sum256(body)
	sums := fmt.Sprintf("%064x  OpenWeir-1.1.0-windows-x64-setup.exe\n%s  %s\n", 0, hex.EncodeToString(digest[:]), name)
	return release{
		pkg:       write(t, filepath.Join(dir, name), body),
		sums:      write(t, filepath.Join(dir, "SHA256SUMS"), []byte(sums)),
		signature: write(t, filepath.Join(dir, "SHA256SUMS.sig"), ed25519.Sign(private, []byte(sums))),
	}
}

func asExit(t *testing.T, err error) int {
	t.Helper()
	exit := (*exitError)(nil)
	if !errors.As(err, &exit) {
		t.Fatalf("%v carries no exit code", err)
	}
	return exit.code
}

func TestTheUpdateKeyIsAnEd25519PublicKeyInBase64(t *testing.T) {
	key, _ := testKey(t)
	if _, err := updateKey(key); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{
		"", // a build without OPENWEIR_LINUX_UPDATE_KEY
		key[:len(key)-4],
		base64.StdEncoding.EncodeToString(make([]byte, 64)),
		strings.TrimRight(key, "="),
		"not base64!",
	} {
		if _, err := updateKey(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestTheSignatureCoversSHA256SUMS(t *testing.T) {
	encoded, private := testKey(t)
	key, err := updateKey(encoded)
	if err != nil {
		t.Fatal(err)
	}
	files := signedRelease(t, private, "OpenWeir-1.1.0-linux-x64.deb", []byte("package"))
	if err := checkSums(key, files.sums, files.signature); err != nil {
		t.Fatal(err)
	}
	other, _ := testKey(t)
	otherKey, _ := updateKey(other)
	if err := checkSums(otherKey, files.sums, files.signature); err == nil {
		t.Error("another key's signature accepted")
	}
	sums, _ := os.ReadFile(files.sums)
	signature, _ := os.ReadFile(files.signature)
	dir := t.TempDir()
	changed := write(t, filepath.Join(dir, "changed"), append(slices.Clone(sums), "0000  extra\n"...))
	if err := checkSums(key, changed, files.signature); err == nil {
		t.Error("a changed SHA256SUMS accepted")
	}
	for _, bad := range [][]byte{signature[:63], append(signature, '\n'), []byte(base64.StdEncoding.EncodeToString(signature))} {
		if err := checkSums(key, files.sums, write(t, filepath.Join(dir, "sig"), bad)); err == nil {
			t.Errorf("a signature of %d bytes accepted", len(bad))
		}
	}
}

// The formats are RFC 8032's, as scripts/sign_sums.py writes them and the UI reads them
// (tests/test_sign_sums.py checks the same vector): the public key's 32 bytes in base64, and
// the signature's 64 bytes as they are. This is §7.1's TEST 2, over the message 0x72.
func TestTheFormatsAreRFC8032s(t *testing.T) {
	public, _ := hex.DecodeString("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
	signature, _ := hex.DecodeString("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da" +
		"085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
	key, err := updateKey(base64.StdEncoding.EncodeToString(public))
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	sums := write(t, filepath.Join(dir, "SHA256SUMS"), []byte{0x72})
	if err := checkSums(key, sums, write(t, filepath.Join(dir, "SHA256SUMS.sig"), signature)); err != nil {
		t.Fatal(err)
	}
}

func TestThePackageMatchesItsLineInSHA256SUMS(t *testing.T) {
	dir := t.TempDir()
	name := "OpenWeir-1.1.0-linux-x64.deb"
	pkg := write(t, filepath.Join(dir, name), []byte("package"))
	digest := sha256.Sum256([]byte("package"))
	good := hex.EncodeToString(digest[:])
	for _, tc := range []struct {
		sums string
		ok   bool
	}{
		{sums: good + "  " + name + "\n", ok: true},
		{sums: strings.ToUpper(good) + " *" + name + "\r\n", ok: true},
		{sums: strings.Repeat("0", 64) + "  " + name + "\n" + good + "  " + name + "\n", ok: true},
		{sums: good + "  " + name + "\n" + strings.Repeat("0", 64) + "  " + name + "\n"},
		{sums: strings.Repeat("0", 64) + "  " + name + "\n"},
		{sums: good + "  OpenWeir-1.1.0-linux-arm64.deb\n"},
		{sums: good[1:] + "  " + name + "\n"},
		{sums: ""},
	} {
		sums := write(t, filepath.Join(dir, "SHA256SUMS"), []byte(tc.sums))
		if err := checkListed(sums, name, pkg); (err == nil) != tc.ok {
			t.Errorf("%q: %v", tc.sums, err)
		}
	}
}

func TestVersionsCompareAsNumbers(t *testing.T) {
	for _, tc := range []struct {
		version, than string
		newer         bool
	}{
		{"1.1.0", "1.0.0", true},
		{"0.10.0", "0.9.9", true},
		{"2.0.0", "1.99.99", true},
		{"1.0.0", "1.0.0", false},
		{"1.0.0", "1.0.1", false},
		{"0.9.0", "1.0.0", false},
	} {
		if newer, err := isNewer(tc.version, tc.than); err != nil || newer != tc.newer {
			t.Errorf("isNewer(%q, %q) = %v, %v", tc.version, tc.than, newer, err)
		}
	}
	for _, tc := range [][2]string{{"1.1", "1.0.0"}, {"1.1.0-1", "1.0.0"}, {"1.1.0", "dev"}, {"v1.1.0", "1.0.0"}} {
		if _, err := isNewer(tc[0], tc[1]); err == nil {
			t.Errorf("isNewer(%q, %q) compared", tc[0], tc[1])
		}
	}
}

func TestTheAssetIsThisMachines(t *testing.T) {
	for _, tc := range []struct {
		arch, format, name, version string
	}{
		{"amd64", "deb", "OpenWeir-1.1.0-linux-x64.deb", "1.1.0"},
		{"arm64", "deb", "OpenWeir-1.1.0-linux-arm64.deb", "1.1.0"},
		{"amd64", "rpm", "OpenWeir-2.10.3-linux-x64.rpm", "2.10.3"},
		{"arm64", "rpm", "OpenWeir-1.1.0-linux-arm64.rpm", "1.1.0"},
		{"amd64", "deb", "OpenWeir-1.1.0-linux-arm64.deb", ""},
		{"amd64", "deb", "OpenWeir-1.1.0-linux-x64.rpm", ""},
		{"amd64", "deb", "OpenWeir-1.1-linux-x64.deb", ""},
		{"amd64", "deb", "OpenWeir-1.1.0-preview.1-linux-x64.deb", ""},
		{"amd64", "deb", "openweir_1.1.0_amd64.deb", ""},
		{"amd64", "deb", "OpenWeir-1.1.0-macos-universal.dmg", ""},
	} {
		version, err := update{arch: tc.arch, format: tc.format}.assetVersion(tc.name)
		if version != tc.version || (err == nil) != (tc.version != "") {
			t.Errorf("%s on %s/%s: %q, %v", tc.name, tc.arch, tc.format, version, err)
		}
	}
}

func TestThePackageIsTheReleasesAndNewer(t *testing.T) {
	deb := update{installed: "1.0.0", arch: "amd64", format: "deb"}
	rpm := update{installed: "1.0.0", arch: "arm64", format: "rpm"}
	for _, tc := range []struct {
		update update
		info   packageInfo
		ok     bool
	}{
		{deb, packageInfo{"openweir", "1.1.0", "amd64"}, true},
		{deb, packageInfo{"openweir", "1.1.0-1", "amd64"}, true}, // nfpm's release
		{rpm, packageInfo{"openweir", "1.1.0", "aarch64"}, true},
		{deb, packageInfo{"openweir-nightly", "1.1.0", "amd64"}, false},
		{deb, packageInfo{"openweir", "1.1.0", "arm64"}, false},
		{rpm, packageInfo{"openweir", "1.1.0", "arm64"}, false}, // rpm calls it aarch64
		{deb, packageInfo{"openweir", "1.2.0", "amd64"}, false}, // not the version its name says
		{deb, packageInfo{"openweir", "1:1.1.0", "amd64"}, false},
		{deb, packageInfo{"openweir", "", "amd64"}, false},
		{update{installed: "1.1.0", arch: "amd64", format: "deb"}, packageInfo{"openweir", "1.1.0", "amd64"}, false},
		{update{installed: "1.2.0", arch: "amd64", format: "deb"}, packageInfo{"openweir", "1.1.0", "amd64"}, false},
		{update{installed: "dev", arch: "amd64", format: "deb"}, packageInfo{"openweir", "1.1.0", "amd64"}, false},
	} {
		if err := tc.update.check(tc.info, "1.1.0"); (err == nil) != tc.ok {
			t.Errorf("%+v over %s: %v", tc.info, tc.update.installed, err)
		}
	}
}

func TestTheCopiesRefuseLinksAndOddFiles(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(t.TempDir(), "copy")
	source := write(t, filepath.Join(dir, "SHA256SUMS"), []byte("sums"))
	if err := copyIn(source, target, 4); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(target); string(data) != "sums" {
		t.Fatalf("copied %q", data)
	}
	if info, _ := os.Stat(target); info.Mode().Perm() != 0o600 {
		t.Errorf("the copy is %v", info.Mode())
	}
	if err := copyIn(source, target, 4); err == nil {
		t.Error("copied over a file that was there")
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(source, link); err != nil {
		t.Fatal(err)
	}
	fifo := filepath.Join(dir, "fifo")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		source string
		limit  int64
		says   string
	}{
		{link, 4, "symbolic link"},
		{fifo, 4, "not a regular file"}, // and it does not wait for a writer
		{dir, 4, "not a regular file"},
		{source, 3, "more than 3 bytes"},
		{filepath.Join(dir, "missing"), 4, "no such file"},
	} {
		target := filepath.Join(t.TempDir(), "copy")
		err := copyIn(tc.source, target, tc.limit)
		if err == nil || !strings.Contains(err.Error(), tc.says) {
			t.Errorf("%s: %v", tc.source, err)
		}
	}
}

func TestTheCopiesGoWhereOnlyRootWrites(t *testing.T) {
	owner := os.Geteuid()
	cache := filepath.Join(t.TempDir(), "openweir")
	dir, err := workDir(cache, owner)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Dir(dir) != cache {
		t.Fatalf("%s is not in %s", dir, cache)
	}
	if info, _ := os.Stat(dir); !info.IsDir() || info.Mode().Perm() != 0o700 {
		t.Fatalf("%s is %v", dir, info.Mode())
	}
	if again, err := workDir(cache, owner); err != nil || again == dir {
		t.Fatalf("a second update has %q, %v", again, err)
	}
	if _, err := workDir(cache, owner+1); err == nil {
		t.Error("someone else's directory accepted")
	}
	if err := os.Chmod(cache, 0o775); err != nil {
		t.Fatal(err)
	}
	if _, err := workDir(cache, owner); err == nil {
		t.Error("a directory others may write to accepted")
	}
	elsewhere := t.TempDir()
	link := filepath.Join(t.TempDir(), "openweir")
	if err := os.Symlink(elsewhere, link); err != nil {
		t.Fatal(err)
	}
	if _, err := workDir(link, owner); err == nil {
		t.Error("a link accepted")
	}
}

func TestThePackageManagerIsTheFormats(t *testing.T) {
	name := "OpenWeir-1.1.0-linux-x64.deb"
	installed := func(paths ...string) func(string) bool {
		return func(path string) bool { return slices.Contains(paths, path) }
	}
	for _, tc := range []struct {
		format string
		exists func(string) bool
		want   string
	}{
		{"deb", installed(aptGet, dnf), "/usr/bin/apt-get install -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o DPkg::Lock::Timeout=60 ./" + name},
		{"rpm", installed(dnf, zypper), "/usr/bin/dnf -y install ./" + name},
		{"rpm", installed(zypper), "/usr/bin/zypper -n install --allow-unsigned-rpm ./" + name},
		{"rpm", installed(aptGet), ""},
		{"deb", installed(dnf), ""},
		{"tar", installed(aptGet, dnf, zypper), ""},
	} {
		command, err := installCommand(tc.format, name, tc.exists)
		if got := strings.Join(command, " "); got != tc.want || (err == nil) != (tc.want != "") {
			t.Errorf("%s: %q, %v", tc.format, got, err)
		}
	}
}

// fakeTools answers the queries with info and records what would run.
type fakeTools struct {
	info    packageInfo
	failRun error
	queries [][]string
	ran     [][]string
	seen    []string // the copies' directory's files while the package manager runs
}

func (f *fakeTools) tools() packageTools {
	return packageTools{
		output: func(name string, args ...string) ([]byte, error) {
			f.queries = append(f.queries, append([]string{name}, args...))
			return fmt.Appendf(nil, "Package: %s\nVersion: %s\nArchitecture: %s\n", f.info.name, f.info.version, f.info.arch), nil
		},
		run: func(dir, name string, args ...string) error {
			f.ran = append(f.ran, append([]string{dir, name}, args...))
			entries, _ := os.ReadDir(dir)
			for _, entry := range entries {
				f.seen = append(f.seen, entry.Name())
			}
			return f.failRun
		},
		exists: func(path string) bool { return path == aptGet },
	}
}

func TestAnUpdateInstallsOnlyWhatChecksOut(t *testing.T) {
	key, private := testKey(t)
	name := "OpenWeir-1.1.0-linux-x64.deb"
	body := []byte("the package")
	machine := func(t *testing.T, tools *fakeTools) update {
		return update{
			key: key, installed: "1.0.0", arch: "amd64", format: "deb",
			cache: filepath.Join(t.TempDir(), "openweir"), owner: os.Geteuid(), tools: tools.tools(),
		}
	}
	good := packageInfo{"openweir", "1.1.0", "amd64"}

	t.Run("installed", func(t *testing.T) {
		tools := &fakeTools{info: good}
		u := machine(t, tools)
		files := signedRelease(t, private, name, body)
		if err := u.install(files.pkg, files.sums, files.signature); err != nil {
			t.Fatal(err)
		}
		if len(tools.queries) != 1 || tools.queries[0][0] != dpkgDeb || !strings.HasPrefix(tools.queries[0][2], u.cache) {
			t.Fatalf("queried %q, not the copy", tools.queries)
		}
		if len(tools.ran) != 1 || filepath.Dir(tools.ran[0][0]) != u.cache || tools.ran[0][1] != aptGet || tools.ran[0][len(tools.ran[0])-1] != "./"+name {
			t.Fatalf("ran %q", tools.ran)
		}
		if want := []string{name, "SHA256SUMS", "SHA256SUMS.sig"}; !slices.Equal(slices.Sorted(slices.Values(tools.seen)), want) {
			t.Fatalf("the package manager saw %q", tools.seen)
		}
		if entries, _ := os.ReadDir(u.cache); len(entries) != 0 {
			t.Fatalf("left %v behind", entries)
		}
	})

	t.Run("the package manager failed", func(t *testing.T) {
		tools := &fakeTools{info: good, failRun: errors.New("exit status 100")}
		u := machine(t, tools)
		files := signedRelease(t, private, name, body)
		err := u.install(files.pkg, files.sums, files.signature)
		if code := asExit(t, err); code != exitInstallFailed {
			t.Fatalf("exit %d: %v", code, err)
		}
		if entries, _ := os.ReadDir(u.cache); len(entries) != 0 {
			t.Fatalf("left %v behind", entries)
		}
	})

	for _, tc := range []struct {
		name  string
		info  packageInfo
		alter func(t *testing.T, u *update, files *release)
	}{
		{name: "no key in this build", alter: func(t *testing.T, u *update, _ *release) { u.key = "" }},
		{name: "another key", alter: func(t *testing.T, u *update, _ *release) { u.key, _ = testKey(t) }},
		{name: "another package", alter: func(t *testing.T, _ *update, files *release) {
			write(t, files.pkg, []byte("another package"))
		}},
		{name: "a link", alter: func(t *testing.T, _ *update, files *release) {
			target := write(t, filepath.Join(t.TempDir(), "elsewhere"), []byte("the package"))
			os.Remove(files.pkg)
			if err := os.Symlink(target, files.pkg); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "no signature", alter: func(t *testing.T, _ *update, files *release) { os.Remove(files.signature) }},
		{name: "a relative path", alter: func(t *testing.T, _ *update, files *release) { files.sums = "SHA256SUMS" }},
		{name: "another machine's", alter: func(t *testing.T, u *update, _ *release) { u.arch = "arm64" }},
		{name: "an older version", alter: func(t *testing.T, u *update, _ *release) { u.installed = "1.1.0" }},
		{name: "another name inside", info: packageInfo{"other", "1.1.0", "amd64"}},
		{name: "another architecture inside", info: packageInfo{"openweir", "1.1.0", "arm64"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			info := tc.info
			if info == (packageInfo{}) {
				info = good
			}
			tools := &fakeTools{info: info}
			u := machine(t, tools)
			files := signedRelease(t, private, name, body)
			if tc.alter != nil {
				tc.alter(t, &u, &files)
			}
			err := u.install(files.pkg, files.sums, files.signature)
			if code := asExit(t, err); code != exitRefused {
				t.Fatalf("exit %d: %v", code, err)
			}
			if len(tools.ran) != 0 {
				t.Fatalf("ran %q", tools.ran)
			}
			if entries, _ := os.ReadDir(u.cache); len(entries) != 0 {
				t.Fatalf("left %v behind", entries)
			}
		})
	}

	t.Run("no package manager", func(t *testing.T) {
		tools := &fakeTools{info: good}
		u := machine(t, tools)
		u.format, name = "rpm", "OpenWeir-1.1.0-linux-x64.rpm"
		files := signedRelease(t, private, name, body)
		err := u.install(files.pkg, files.sums, files.signature)
		if exit := (*exitError)(nil); err == nil || errors.As(err, &exit) {
			t.Fatalf("%v", err)
		}
		if _, err := os.Stat(u.cache); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("copied before it knew: %v", err)
		}
	})
}

// dpkg-deb's answer is read from a real package; the CI runners and Debian have dpkg-deb.
func TestDpkgDebSaysWhatAPackageIs(t *testing.T) {
	if _, err := os.Stat(dpkgDeb); err != nil {
		t.Skip("no dpkg-deb on this machine")
	}
	root := filepath.Join(t.TempDir(), "root")
	control := "Package: openweir\nVersion: 1.1.0-1\nArchitecture: arm64\nMaintainer: OpenWeir <openweir@example.com>\nDescription: a test package\n"
	if err := os.MkdirAll(filepath.Join(root, "DEBIAN"), 0o755); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(root, "DEBIAN", "control"), []byte(control))
	// dpkg-deb wants the usual modes, whatever the umask.
	for _, path := range []string{root, filepath.Join(root, "DEBIAN")} {
		if err := os.Chmod(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chmod(filepath.Join(root, "DEBIAN", "control"), 0o644); err != nil {
		t.Fatal(err)
	}
	pkg := filepath.Join(t.TempDir(), "OpenWeir-1.1.0-linux-arm64.deb")
	if out, err := exec.Command(dpkgDeb, "--root-owner-group", "--build", root, pkg).CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, out)
	}
	info, err := systemTools().inspect("deb", pkg)
	if err != nil {
		t.Fatal(err)
	}
	if want := (packageInfo{"openweir", "1.1.0-1", "arm64"}); info != want {
		t.Fatalf("%+v, not %+v", info, want)
	}
	if err := (update{installed: "1.0.0", arch: "arm64", format: "deb"}).check(info, "1.1.0"); err != nil {
		t.Fatal(err)
	}
}

func TestRpmAnswersThreeLines(t *testing.T) {
	answer := "openweir\n1.1.0\naarch64\n"
	tools := packageTools{output: func(name string, args ...string) ([]byte, error) {
		if name != rpmTool || !slices.Equal(args[:3], []string{"-qp", "--qf", `%{NAME}\n%{VERSION}\n%{ARCH}\n`}) {
			t.Fatalf("ran %s %q", name, args)
		}
		return []byte(answer), nil
	}}
	if info, err := tools.inspect("rpm", "/var/cache/openweir/update-1/p.rpm"); err != nil || info != (packageInfo{"openweir", "1.1.0", "aarch64"}) {
		t.Fatalf("%+v, %v", info, err)
	}
	answer = "openweir\n1.1.0\n"
	if _, err := tools.inspect("rpm", "/p.rpm"); err == nil {
		t.Fatal("two lines accepted")
	}
}

func TestThePackageFormatIsTheMarkers(t *testing.T) {
	dir := t.TempDir()
	for _, tc := range []struct{ marker, format string }{{"deb\n", "deb"}, {"rpm", "rpm"}, {"tar\n", ""}, {"", ""}} {
		format, err := packageFormat(write(t, filepath.Join(dir, "package-format"), []byte(tc.marker)))
		if format != tc.format || (err == nil) != (tc.format != "") {
			t.Errorf("%q: %q, %v", tc.marker, format, err)
		}
	}
	if _, err := packageFormat(filepath.Join(dir, "missing")); err == nil {
		t.Error("no marker accepted")
	}
}

// Run by anyone else, the helper stops before it reads a file; as root, without pkexec too.
func TestTheUpdateHelperRunsAsRootThroughPkexec(t *testing.T) {
	err := installUpdate("/tmp/OpenWeir-1.1.0-linux-x64.deb", "/tmp/SHA256SUMS", "/tmp/SHA256SUMS.sig")
	if os.Geteuid() != 0 {
		if err == nil || !strings.Contains(err.Error(), "runs as root") {
			t.Fatalf("as uid %d: %v", os.Geteuid(), err)
		}
		return
	}
	if os.Getenv("PKEXEC_UID") != "" {
		t.Skip("run through pkexec")
	}
	if err == nil || !strings.Contains(err.Error(), "pkexec") {
		t.Fatalf("as root without pkexec: %v", err)
	}
}

// main exits with the code an error carries through any wrapping, and 1 otherwise.
func TestTheExitCodeSurvivesWrapping(t *testing.T) {
	err := fmt.Errorf("outer: %w", refused(errors.New("bad signature")))
	if code := asExit(t, err); code != exitRefused {
		t.Fatalf("exit %d", code)
	}
	if !strings.Contains(err.Error(), "bad signature") {
		t.Fatalf("%v", err)
	}
}
