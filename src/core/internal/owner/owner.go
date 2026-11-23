// Package owner is the Windows service's owner (docs/architecture.md §5.4): who may use the
// core, their mihomo home, what the service keeps there to bring the tunnel back when they
// sign in again (§5.2), and their system proxy (§5.5).
//
// One service process serves one owner. mihomo opens cache.db in its home once per process
// (§3.2), so a claim by another user records them in owner.json and restarts the service,
// which opens the engine in their home. The package itself runs anywhere; the Windows and
// Linux parts come in through Platform, and users are SIDs on Windows and decimal uids on
// Linux (validUserID).
package owner

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// The layout under the service's root, %ProgramData%\OpenWeir (§5.1).
const (
	recordFile = "owner.json"
	usersDir   = "users"
)

// Record is owner.json: the owner's SID and the session they last claimed or signed in from.
// On Linux SID holds the owner's decimal uid, and their session is that uid too: the service
// follows logind's users, not their sessions (docs/architecture.md "Linux (in progress)").
type Record struct {
	SID     string `json:"sid"`
	Session uint32 `json:"session"`
	// LogonTime scopes recovery to the login that last connected, not a reused session ID.
	LogonTime int64 `json:"logon_time,omitempty"`
}

// ValidSID accepts the string form of a SID ("S-1-5-21-…"), which names the owner's home
// directory, so nothing else may get through.
func ValidSID(sid string) bool {
	parts, ok := strings.CutPrefix(sid, "S-1-")
	if !ok || parts == "" {
		return false
	}
	for part := range strings.SplitSeq(parts, "-") {
		if part == "" || len(part) > 10 || strings.Trim(part, "0123456789") != "" {
			return false
		}
	}
	return true
}

// ValidUID accepts a Linux uid in decimal ("1000"), which names the owner's home directory as
// a SID does on Windows. It refuses what would name another directory for the same uid, such
// as leading zeros, and (uid_t)-1, which is no one.
func ValidUID(uid string) bool {
	if uid == "" || len(uid) > 10 || strings.Trim(uid, "0123456789") != "" || (uid[0] == '0' && uid != "0") {
		return false
	}
	number, err := strconv.ParseUint(uid, 10, 32)
	return err == nil && number != math.MaxUint32
}

// Home is the owner's mihomo home under root.
func Home(root, sid string) string {
	return filepath.Join(root, usersDir, sid)
}

// LoadRecord reads owner.json; false when the service has no owner yet.
func LoadRecord(root string) (Record, bool, error) {
	raw, err := os.ReadFile(filepath.Join(root, recordFile))
	if errors.Is(err, fs.ErrNotExist) {
		return Record{}, false, nil
	}
	if err != nil {
		return Record{}, false, err
	}
	var record Record
	if err := json.Unmarshal(raw, &record); err != nil {
		return Record{}, false, fmt.Errorf("%s: %w", recordFile, err)
	}
	if !validUserID(record.SID) {
		return Record{}, false, fmt.Errorf("%s: %q is not a user ID", recordFile, record.SID)
	}
	return record, true, nil
}

// SaveRecord replaces owner.json.
func SaveRecord(root string, record Record) error {
	raw, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return writeFile(filepath.Join(root, recordFile), raw)
}

// RemoveRecord deletes owner.json, leaving the service with no owner; nothing to delete is no
// error.
func RemoveRecord(root string) error {
	if err := os.Remove(filepath.Join(root, recordFile)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// writeFile replaces path in one step: a new file that only this process opened, then a
// rename over the old one, so a crash leaves either version and never half of one.
func writeFile(path string, data []byte) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	_, err = temporary.Write(data)
	if err == nil {
		err = temporary.Sync()
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return replaceFile(temporary.Name(), path)
}
