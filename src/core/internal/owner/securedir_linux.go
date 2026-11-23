package owner

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"

	"golang.org/x/sys/unix"
)

// How the Linux service secures its directories, as SecureDir does with ACLs on Windows: root
// alone writes them. A home's owner reads it through a POSIX ACL entry for their uid, which its
// default ACL hands down to what the service and mihomo create inside; nobody else reads it. A
// directory without a reader, such as the root and users/, lets others pass through to the
// home they may read but not list it: Windows skips the check on the way there, Linux needs
// the search bit on every directory above (so systemd's StateDirectoryMode is 0711 too).
const (
	passMode = 0o711
	homeMode = 0o700
)

// dirs secures directories as owner, root in the service; tests run as themselves.
type dirs struct {
	owner uint32
	// setxattr and removexattr reach the directory's ACLs; tests stand in for them where the
	// file system has none.
	setxattr    func(fd int, name string, data []byte) error
	removexattr func(fd int, name string) error
}

var rootDirs = dirs{
	owner:       0,
	setxattr:    func(fd int, name string, data []byte) error { return unix.Fsetxattr(fd, name, data, 0) },
	removexattr: unix.Fremovexattr,
}

// The extended attributes that hold a file's ACLs.
const (
	accessACL  = "system.posix_acl_access"
	defaultACL = "system.posix_acl_default"
)

// secure is SecureDir: it creates dir if needed and works on the directory it opened, so that
// nothing swapped in after the check is changed instead.
func (d dirs) secure(dir, reader string) error {
	var uid uint32
	if reader != "" {
		var ok bool
		if uid, ok = parseUID(reader); !ok {
			return fmt.Errorf("%q is not a uid", reader)
		}
	}
	if err := os.Mkdir(dir, homeMode); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	fd, err := unix.Open(dir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if errors.Is(err, unix.ELOOP) || errors.Is(err, unix.ENOTDIR) {
		return fmt.Errorf("%s is not a plain directory", dir)
	} else if err != nil {
		return fmt.Errorf("open %s: %w", dir, err)
	}
	defer unix.Close(fd)
	// Someone else's directory could have links in it already for the service to follow, and
	// they could change its mode again.
	var stat unix.Stat_t
	if err := unix.Fstat(fd, &stat); err != nil {
		return fmt.Errorf("inspect %s: %w", dir, err)
	}
	if stat.Uid != d.owner {
		return fmt.Errorf("%s belongs to uid %d, not to root", dir, stat.Uid)
	}
	if reader == "" {
		// An ACL left from before would outlast the mode.
		for _, name := range []string{accessACL, defaultACL} {
			if err := d.removexattr(fd, name); err != nil && !errors.Is(err, unix.ENODATA) && !unsupported(err) {
				return fmt.Errorf("clear the ACL of %s: %w", dir, err)
			}
		}
		if err := unix.Fchmod(fd, passMode); err != nil {
			return fmt.Errorf("secure %s: %w", dir, err)
		}
		return nil
	}
	if err := unix.Fchmod(fd, homeMode); err != nil {
		return fmt.Errorf("secure %s: %w", dir, err)
	}
	acl := readerACL(uid)
	for _, name := range []string{accessACL, defaultACL} {
		err := d.setxattr(fd, name, acl)
		if unsupported(err) {
			// The home stays the service's alone: the owner cannot read their crash reports.
			slog.Warn("the file system has no ACLs; the owner cannot read their home", "dir", dir, "error", err)
			return nil
		} else if err != nil {
			return fmt.Errorf("let user %d read %s: %w", uid, dir, err)
		}
	}
	return nil
}

func unsupported(err error) bool {
	return errors.Is(err, unix.EOPNOTSUPP) || errors.Is(err, unix.ENOTSUP)
}

// The POSIX ACL extended attribute's format (linux/posix_acl_xattr.h): a little-endian version
// 2, then entries of a tag, permissions and an ID, ordered by tag.
const (
	aclVersion   = 2
	aclUserObj   = 0x01
	aclUser      = 0x02
	aclGroupObj  = 0x04
	aclMask      = 0x10
	aclOther     = 0x20
	aclUndefined = 0xffffffff

	aclRead    = 4
	aclWrite   = 2
	aclExecute = 1
)

// readerACL lets root do anything and uid read and search, and nobody else anything. The
// mask lets uid's entry through; it becomes the group bits of the mode, which is 0750.
func readerACL(uid uint32) []byte {
	entries := []struct {
		tag, permissions uint16
		id               uint32
	}{
		{aclUserObj, aclRead | aclWrite | aclExecute, aclUndefined},
		{aclUser, aclRead | aclExecute, uid},
		{aclGroupObj, 0, aclUndefined},
		{aclMask, aclRead | aclExecute, aclUndefined},
		{aclOther, 0, aclUndefined},
	}
	acl := binary.LittleEndian.AppendUint32(nil, aclVersion)
	for _, entry := range entries {
		acl = binary.LittleEndian.AppendUint16(acl, entry.tag)
		acl = binary.LittleEndian.AppendUint16(acl, entry.permissions)
		acl = binary.LittleEndian.AppendUint32(acl, entry.id)
	}
	return acl
}
