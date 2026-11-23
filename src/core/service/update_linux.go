package main

import (
	"bufio"
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"github.com/openweir/app-desktop/src/core/internal/product"
)

// The UI's shell passes -install-update's exit code to the UI it starts next, as
// --update-result, and src/openweir/update/linux.py says what it means. 0 is installed and 1
// is a helper that could not start on its work (not root, not through pkexec, no
// package-format marker, no package manager or no safe directory for the copies); pkexec
// itself exits 126 when the user dismissed its dialog and 127 when it could not authorize.
// Only exitInstallFailed comes after the package manager started: with every other code,
// nothing was installed.
const (
	// exitRefused: the files did not check out, so nothing was installed.
	exitRefused = 3
	// exitInstallFailed: the package manager ran and failed.
	exitInstallFailed = 4
)

const (
	// packageName is the package's own name, which package/linux gives it.
	packageName = "openweir"
	// packageFormatName, beside the executable, holds the format the package was: deb or rpm.
	packageFormatName = "package-format"
	// updateCache takes the copies the helper checks and installs: the units' CacheDirectory,
	// which only root writes.
	updateCache = "/var/cache/openweir"
	sumsName    = "SHA256SUMS"
	// The limits the UI downloads with (src/openweir/update): a package of at most 1 GiB and a
	// SHA256SUMS of at most 64 KiB.
	maxPackageBytes = 1 << 30
	maxSumsBytes    = 64 << 10
)

// The query tools and package managers, where the distributions install them: the helper runs
// nothing that PATH names.
const (
	dpkgDeb = "/usr/bin/dpkg-deb"
	rpmTool = "/usr/bin/rpm"
	aptGet  = "/usr/bin/apt-get"
	dnf     = "/usr/bin/dnf"
	zypper  = "/usr/bin/zypper"
)

// toolPath is PATH for what the package managers run in turn, such as maintainer scripts.
const toolPath = "PATH=/usr/sbin:/usr/bin:/sbin:/bin"

var releaseVersion = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)$`)

// debianRevision is the -<release> that nfpm may append to a .deb's version.
var debianRevision = regexp.MustCompile(`-\d+$`)

// sumsLine is a line of sha256sum's output: the digest, then " " or " *" and the name.
var sumsLine = regexp.MustCompile(`^([0-9a-fA-F]{64}) [ *](.+)$`)

// installUpdate installs the package at pkg as root once sums, a release's SHA256SUMS, checks
// out against signature with product.LinuxUpdateKey and pkg against sums (D62). pkexec runs it
// for the UI, whose shell then starts the UI again with --update-result=<this exit code>.
func installUpdate(pkg, sums, signature string) error {
	if err := requireRoot("-install-update", os.Geteuid()); err != nil {
		return err
	}
	if err := ranByPkexec("-install-update", os.Getenv("PKEXEC_UID")); err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	format, err := packageFormat(filepath.Join(filepath.Dir(executable), packageFormatName))
	if err != nil {
		return err
	}
	return update{
		key:       product.LinuxUpdateKey,
		installed: product.Version,
		arch:      runtime.GOARCH,
		format:    format,
		cache:     updateCache,
		owner:     0,
		tools:     systemTools(),
	}.install(pkg, sums, signature)
}

// update is what -install-update holds a package to, and the tools it uses; tests stand in
// for the machine.
type update struct {
	// key is the Ed25519 public key that signs SHA256SUMS, in base64.
	key string
	// installed is the version installed: this executable's.
	installed string
	// arch is the installed package's architecture, as GOARCH names it.
	arch string
	// format is the installed package's: deb or rpm.
	format string
	// cache takes the copies; owner must own it.
	cache string
	owner int
	tools packageTools
}

// packageTools runs the package tools; tests record what it is asked instead.
type packageTools struct {
	// output runs a query and returns what it wrote to standard output.
	output func(name string, args ...string) ([]byte, error)
	// run runs a package manager in dir; it writes to this process's output.
	run func(dir, name string, args ...string) error
	// exists says whether a package manager is installed.
	exists func(path string) bool
}

// copied is one of the files the user named, and where its copy goes.
type copied struct {
	source, copy string
	limit        int64
}

// install copies the files, checks them and installs the package. The copies go into a
// directory only root can write to, so that the user cannot swap a file between its check and
// its install, and are removed when it is done.
func (u update) install(pkg, sums, signature string) error {
	key, err := updateKey(u.key)
	if err != nil {
		return refused(err)
	}
	name := filepath.Base(pkg)
	version, err := u.assetVersion(name)
	if err != nil {
		return refused(err)
	}
	for _, path := range []string{pkg, sums, signature} {
		if !filepath.IsAbs(path) {
			return refused(fmt.Errorf("%q is not an absolute path", path))
		}
	}
	// Before the copying: a package manager that is missing stays missing.
	command, err := installCommand(u.format, name, u.tools.exists)
	if err != nil {
		return err
	}
	dir, err := workDir(u.cache, u.owner)
	if err != nil {
		return err
	}
	defer func() {
		if err := os.RemoveAll(dir); err != nil {
			fmt.Fprintln(os.Stderr, "remove the update's copies:", err)
		}
	}()
	files := []copied{
		{source: pkg, copy: filepath.Join(dir, name), limit: maxPackageBytes},
		{source: sums, copy: filepath.Join(dir, sumsName), limit: maxSumsBytes},
		{source: signature, copy: filepath.Join(dir, sumsName+".sig"), limit: ed25519.SignatureSize},
	}
	for _, file := range files {
		if err := copyIn(file.source, file.copy, file.limit); err != nil {
			return refused(err)
		}
	}
	pkgCopy, sumsCopy, signatureCopy := files[0].copy, files[1].copy, files[2].copy
	if err := checkSums(key, sumsCopy, signatureCopy); err != nil {
		return refused(err)
	}
	if err := checkListed(sumsCopy, name, pkgCopy); err != nil {
		return refused(err)
	}
	info, err := u.tools.inspect(u.format, pkgCopy)
	if err != nil {
		return refused(err)
	}
	if err := u.check(info, version); err != nil {
		return refused(err)
	}
	if err := u.tools.run(dir, command[0], command[1:]...); err != nil {
		return &exitError{code: exitInstallFailed, err: fmt.Errorf("-install-update: %s: %w", filepath.Base(command[0]), err)}
	}
	return nil
}

func refused(err error) error {
	return &exitError{code: exitRefused, err: fmt.Errorf("-install-update: %w", err)}
}

// packageFormat reads the marker the package leaves beside the executable.
func packageFormat(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("-install-update: the package format: %w", err)
	}
	format := strings.TrimSpace(string(data))
	if format != "deb" && format != "rpm" {
		return "", fmt.Errorf("-install-update: %s says %q, not deb or rpm", path, format)
	}
	return format, nil
}

// updateKey decodes product.LinuxUpdateKey: an Ed25519 public key's 32 bytes in standard
// base64, as scripts/sign_sums.py prints it.
func updateKey(text string) (ed25519.PublicKey, error) {
	if text == "" {
		return nil, errors.New("this build has no update key (OPENWEIR_LINUX_UPDATE_KEY)")
	}
	raw, err := base64.StdEncoding.Strict().DecodeString(text)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("the update key is not an Ed25519 public key's %d bytes in base64", ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(raw), nil
}

// machine is an architecture as the release's assets name it.
func machine(goarch string) string {
	if goarch == "amd64" {
		return "x64"
	}
	return goarch
}

// assetVersion is the version in name, which must be this machine's asset:
// <DisplayName>-<version>-linux-<x64|arm64>.<deb|rpm>.
func (u update) assetVersion(name string) (string, error) {
	prefix := product.DisplayName + "-"
	suffix := "-linux-" + machine(u.arch) + "." + u.format
	version, ok := strings.CutPrefix(name, prefix)
	if ok {
		version, ok = strings.CutSuffix(version, suffix)
	}
	if !ok || !releaseVersion.MatchString(version) {
		return "", fmt.Errorf("%q is not a release's package for this machine (%s<version>%s)", name, prefix, suffix)
	}
	return version, nil
}

// installCommand installs ./name from the copies' directory with the installed format's
// package manager: apt-get, which brings new dependencies too, and dnf, or zypper where there
// is no dnf (openSUSE). Nothing asks, as there is no one to answer: the update key, not a
// distribution's, vouches for the package, so zypper takes it unsigned, and dpkg keeps a
// configuration file that was changed.
func installCommand(format, name string, exists func(string) bool) ([]string, error) {
	file := "./" + name
	switch {
	case format == "deb" && exists(aptGet):
		return []string{aptGet, "install", "-y",
			"-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
			// Ubuntu's unattended upgrades hold the lock now and then.
			"-o", "DPkg::Lock::Timeout=60",
			file}, nil
	case format == "rpm" && exists(dnf):
		return []string{dnf, "-y", "install", file}, nil
	case format == "rpm" && exists(zypper):
		return []string{zypper, "-n", "install", "--allow-unsigned-rpm", file}, nil
	case format == "deb":
		return nil, fmt.Errorf("-install-update: %s is not installed", aptGet)
	case format == "rpm":
		return nil, fmt.Errorf("-install-update: neither %s nor %s is installed", dnf, zypper)
	}
	return nil, fmt.Errorf("-install-update: no package manager for %q", format)
}

// workDir makes a new directory for the copies in cache, which must be a directory that owner
// (root) owns and no one else may write to. systemd makes cache for the service; the helper
// makes it as systemd would where the service has not run yet.
func workDir(cache string, owner int) (string, error) {
	if err := os.Mkdir(cache, 0o755); err != nil && !errors.Is(err, fs.ErrExist) {
		return "", err
	}
	info, err := os.Lstat(cache)
	if err != nil {
		return "", err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	switch {
	case !info.IsDir():
		return "", fmt.Errorf("-install-update: %s is not a directory", cache)
	case !ok || int(stat.Uid) != owner:
		return "", fmt.Errorf("-install-update: %s does not belong to uid %d", cache, owner)
	case info.Mode().Perm()&0o022 != 0:
		return "", fmt.Errorf("-install-update: others than its owner may write to %s", cache)
	}
	// MkdirTemp makes it 0700.
	return os.MkdirTemp(cache, "update-")
}

// copyIn copies the file the user named at source to target, a new file. It refuses a symbolic
// link (O_NOFOLLOW), anything but a regular file and more than limit bytes; O_NONBLOCK keeps a
// FIFO from holding it up.
func copyIn(source, target string, limit int64) error {
	in, err := os.OpenFile(source, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if errors.Is(err, syscall.ELOOP) {
		return fmt.Errorf("%s is a symbolic link", source)
	}
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s is not a regular file", source)
	}
	if info.Size() > limit {
		return fmt.Errorf("%s has more than %d bytes", source, limit)
	}
	out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	// The file may have grown since the Stat.
	n, err := io.Copy(out, io.LimitReader(in, limit+1))
	if closeErr := out.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("copy %s: %w", source, err)
	}
	if n > limit {
		return fmt.Errorf("%s has more than %d bytes", source, limit)
	}
	return nil
}

// checkSums checks the signature on SHA256SUMS: Ed25519 over the file's bytes, the signature's
// 64 bytes as they are (scripts/sign_sums.py).
func checkSums(key ed25519.PublicKey, sums, signature string) error {
	body, err := os.ReadFile(sums)
	if err != nil {
		return err
	}
	sig, err := os.ReadFile(signature)
	if err != nil {
		return err
	}
	if len(sig) != ed25519.SignatureSize {
		return fmt.Errorf("%s.sig has %d bytes, not an Ed25519 signature's %d", sumsName, len(sig), ed25519.SignatureSize)
	}
	if !ed25519.Verify(key, body, sig) {
		return fmt.Errorf("%s.sig is not the update key's signature of %s", sumsName, sumsName)
	}
	return nil
}

// checkListed checks the file at path against name's line in SHA256SUMS. As in the UI
// (src/openweir/update/download.py), a name listed twice goes by its last line.
func checkListed(sums, name, path string) error {
	body, err := os.ReadFile(sums)
	if err != nil {
		return err
	}
	want := ""
	lines := bufio.NewScanner(bytes.NewReader(body))
	for lines.Scan() {
		match := sumsLine.FindStringSubmatch(strings.TrimSpace(lines.Text()))
		if match != nil && strings.TrimSpace(match[2]) == name {
			want = strings.ToLower(match[1])
		}
	}
	if err := lines.Err(); err != nil {
		return err
	}
	if want == "" {
		return fmt.Errorf("%s does not list %s", sumsName, name)
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return err
	}
	if got := hex.EncodeToString(digest.Sum(nil)); got != want {
		return fmt.Errorf("%s has SHA-256 %s, not the %s that %s lists", name, got, want, sumsName)
	}
	return nil
}

// packageInfo is what a package says of itself.
type packageInfo struct {
	name, version, arch string
}

// inspect asks dpkg-deb or rpm what the package at path is. It runs on the copy once its
// SHA-256 is the signed one.
func (t packageTools) inspect(format, path string) (packageInfo, error) {
	switch format {
	case "deb":
		out, err := t.output(dpkgDeb, "-f", path, "Package", "Version", "Architecture")
		if err != nil {
			return packageInfo{}, fmt.Errorf("dpkg-deb: %w", err)
		}
		fields := controlFields(out)
		return packageInfo{name: fields["Package"], version: fields["Version"], arch: fields["Architecture"]}, nil
	case "rpm":
		out, err := t.output(rpmTool, "-qp", "--qf", `%{NAME}\n%{VERSION}\n%{ARCH}\n`, path)
		if err != nil {
			return packageInfo{}, fmt.Errorf("rpm: %w", err)
		}
		lines := strings.Split(strings.TrimSuffix(string(out), "\n"), "\n")
		if len(lines) != 3 {
			return packageInfo{}, fmt.Errorf("rpm answered %q", out)
		}
		return packageInfo{name: lines[0], version: lines[1], arch: lines[2]}, nil
	}
	return packageInfo{}, fmt.Errorf("no tool reads a %q package", format)
}

// controlFields reads dpkg-deb -f's answer for several fields: a "Field: value" line each.
func controlFields(out []byte) map[string]string {
	fields := map[string]string{}
	for line := range strings.SplitSeq(string(out), "\n") {
		if name, value, ok := strings.Cut(line, ":"); ok {
			fields[strings.TrimSpace(name)] = strings.TrimSpace(value)
		}
	}
	return fields
}

// packageArch is GOARCH as each format names the architecture.
var packageArch = map[string]map[string]string{
	"deb": {"amd64": "amd64", "arm64": "arm64"},
	"rpm": {"amd64": "x86_64", "arm64": "aarch64"},
}

// check holds the package to OpenWeir for this machine, at the version its asset's name says
// and newer than the one installed: an older release is signed too, and must not take a newer
// one's place.
func (u update) check(info packageInfo, assetVersion string) error {
	if info.name != packageName {
		return fmt.Errorf("the package is %q, not %s", info.name, packageName)
	}
	if want := packageArch[u.format][u.arch]; info.arch != want {
		return fmt.Errorf("the package is for %q, not %s", info.arch, want)
	}
	version := info.version
	if u.format == "deb" {
		version = debianRevision.ReplaceAllString(version, "")
	}
	if version != assetVersion {
		return fmt.Errorf("the package is version %q, not the %s its name says", info.version, assetVersion)
	}
	newer, err := isNewer(version, u.installed)
	if err != nil {
		return err
	}
	if !newer {
		return fmt.Errorf("%s is not newer than the installed %s", version, u.installed)
	}
	return nil
}

// isNewer compares two X.Y.Z versions as numbers, as the UI does.
func isNewer(version, than string) (bool, error) {
	next, ok := parseVersion(version)
	if !ok {
		return false, fmt.Errorf("%q is not a release version", version)
	}
	current, ok := parseVersion(than)
	if !ok {
		return false, fmt.Errorf("the installed version %q is not a release version", than)
	}
	for i := range next {
		if next[i] != current[i] {
			return next[i] > current[i], nil
		}
	}
	return false, nil
}

func parseVersion(text string) ([3]int, bool) {
	var parts [3]int
	match := releaseVersion.FindStringSubmatch(text)
	if match == nil {
		return parts, false
	}
	for i := range parts {
		n, err := strconv.Atoi(match[i+1])
		if err != nil {
			return parts, false
		}
		parts[i] = n
	}
	return parts, true
}

// systemTools runs the real tools, in English (LC_ALL=C), and asks nothing: debconf's
// questions go to its noninteractive frontend, and standard input is /dev/null.
func systemTools() packageTools {
	environment := append(os.Environ(), toolPath, "LC_ALL=C", "DEBIAN_FRONTEND=noninteractive")
	return packageTools{
		output: func(name string, args ...string) ([]byte, error) {
			command := exec.Command(name, args...)
			command.Env = environment
			command.Stderr = os.Stderr
			return command.Output()
		},
		run: func(dir, name string, args ...string) error {
			command := exec.Command(name, args...)
			command.Dir = dir
			command.Env = environment
			command.Stdout, command.Stderr = os.Stdout, os.Stderr
			return command.Run()
		},
		exists: func(path string) bool {
			info, err := os.Stat(path)
			return err == nil && info.Mode().IsRegular()
		},
	}
}
