"""
Workspace sync: local workspace <-> Dropbox.

Usage:
    python workspace_sync.py sync-workspace [--yes]  # local <-> Dropbox (bidirectional)
    python workspace_sync.py push-workspace [--yes]  # local -> Dropbox
    python workspace_sync.py pull-workspace [--yes]  # Dropbox -> local
    python workspace_sync.py status                  # show what's changed
    python workspace_sync.py init                    # create config file

    --yes (-y): skip confirmation prompt (still stops if there are conflicts)

Reads .syncignore (in local workspace) for exclusion patterns.
Always previews before syncing. Asks on conflicts.
Uses size + content hash for change detection (mtime ignored — unreliable
across machines and copy operations).

Safety guarantees:
- NEVER deletes files automatically. Deletions are always surfaced as
  conflicts requiring explicit user choice (must type 'delete', not a
  single keystroke).
- Push never AUTOMATICALLY modifies or deletes local files. Pull never
  AUTOMATICALLY modifies or deletes Dropbox files. User-resolved conflicts
  can modify either side, but only through explicit choice.
- First sync with files on both sides hashes to detect true differences
  (never assumes same-size means same-content).
"""

import os
import sys
import json
import shutil
import hashlib
import fnmatch
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "sync-config.json"
MANIFEST_NAME = ".sync-manifest.json"
SYNCIGNORE_NAME = ".syncignore"


def load_config() -> tuple[Path, Path]:
    """Load local and remote roots from config file."""
    if not CONFIG_PATH.exists():
        print(f"No config found. Run 'python workspace_sync.py init' first.")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    return Path(cfg["local_root"]), Path(cfg["remote_root"])


def save_config(local_root: Path, remote_root: Path):
    with open(CONFIG_PATH, "w") as f:
        json.dump({
            "local_root": str(local_root),
            "remote_root": str(remote_root),
        }, f, indent=2)


def cmd_init():
    """Interactive setup: ask for local and remote paths."""
    print("Workspace sync setup\n")

    default_remote = "~/Dropbox/claude-code-workspace"
    local = input("Local workspace path (e.g. C:/Users/you/Claude Code): ").strip()
    if not local:
        print("Aborted.")
        return
    local_path = Path(local).expanduser().resolve()
    if not local_path.exists():
        print(f"Warning: {local_path} does not exist yet.")

    remote = input(f"Dropbox workspace path [{default_remote}]: ").strip()
    if not remote:
        remote = default_remote
    remote_path = Path(remote).expanduser().resolve()

    save_config(local_path, remote_path)
    print(f"\nConfig saved to {CONFIG_PATH}")
    print(f"  Local:  {local_path}")
    print(f"  Remote: {remote_path}")

    # Copy default .syncignore if none exists in the local workspace
    syncignore_dest = local_path / SYNCIGNORE_NAME
    syncignore_default = SCRIPT_DIR / ".syncignore.default"
    if not syncignore_dest.exists() and syncignore_default.exists():
        local_path.mkdir(parents=True, exist_ok=True)
        shutil.copy2(syncignore_default, syncignore_dest)
        print(f"  Copied default .syncignore to {syncignore_dest}")
    elif not syncignore_dest.exists():
        print(f"  Note: create a .syncignore in {local_path} to exclude files.")
    else:
        print(f"  .syncignore already exists at {syncignore_dest}")

    print("\nReady. Run 'push-workspace' or 'pull-workspace' to sync.")


# ---------------------------------------------------------------------------
# .syncignore
# ---------------------------------------------------------------------------

def load_syncignore(local_root: Path) -> tuple[list[str], list[str]]:
    """
    Load .syncignore from local_root.
    Returns (dir_patterns, file_patterns).
    """
    dir_patterns = []
    file_patterns = []
    ignore_path = local_root / SYNCIGNORE_NAME
    if not ignore_path.exists():
        return dir_patterns, file_patterns
    with open(ignore_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("/"):
                dir_patterns.append(line.rstrip("/"))
            else:
                file_patterns.append(line)
    return dir_patterns, file_patterns


def should_skip(rel_path: Path, dir_patterns: list[str], file_patterns: list[str]) -> bool:
    """Check if a relative path should be excluded."""
    rel_str = str(rel_path)

    # Always skip manifest and machine-specific config (NOT .syncignore —
    # that should sync so both machines use the same exclusion patterns)
    if rel_path.name in (MANIFEST_NAME, "sync-config.json"):
        return True

    # Skip Windows reserved device names (NUL, CON, PRN, AUX, etc.)
    RESERVED = {"NUL", "CON", "PRN", "AUX", "COM1", "COM2", "COM3", "COM4",
                "LPT1", "LPT2", "LPT3"}
    if rel_path.stem.upper() in RESERVED:
        return True

    # Check directory patterns: if any component matches, skip
    for part in rel_path.parts:
        for dp in dir_patterns:
            if fnmatch.fnmatch(part, dp):
                return True
    # Path-style dir patterns (e.g. "riemannian-latent-geometry/")
    for dp in dir_patterns:
        if "/" in dp or "\\" in dp:
            dp_normalized = dp.replace("\\", "/")
            rel_normalized = rel_str.replace("\\", "/")
            if rel_normalized.startswith(dp_normalized + "/") or rel_normalized == dp_normalized:
                return True

    # File patterns
    for fp in file_patterns:
        if "/" in fp or "\\" in fp:
            fp_normalized = fp.replace("\\", "/")
            rel_normalized = rel_str.replace("\\", "/")
            if fnmatch.fnmatch(rel_normalized, fp_normalized):
                return True
        else:
            if fnmatch.fnmatch(rel_path.name, fp):
                return True

    return False


# ---------------------------------------------------------------------------
# Scanning and hashing
# ---------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    """SHA-256 hash of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(root: Path, dir_patterns: list[str], file_patterns: list[str]) -> dict[str, dict]:
    """Scan directory tree. Returns {relative_path: {size}}."""
    entries = {}
    if not root.exists():
        return entries

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)

        # Prune skipped directories
        dirnames[:] = [
            d for d in dirnames
            if not should_skip(rel_dir / d, dir_patterns, file_patterns)
        ]

        for fname in filenames:
            rel = rel_dir / fname
            if should_skip(rel, dir_patterns, file_patterns):
                continue
            full = Path(dirpath) / fname
            try:
                stat = full.stat()
                entries[str(rel)] = {"size": stat.st_size}
            except OSError:
                continue

    return entries


# ---------------------------------------------------------------------------
# Manifest: stores hashes of all synced files
# ---------------------------------------------------------------------------

def load_manifest(root: Path) -> dict:
    manifest_path = root / MANIFEST_NAME
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            return json.load(f)
    return {}


def save_manifests(files_dict: dict, local_root: Path, remote_root: Path):
    """Save manifest to both roots."""
    now = datetime.now(timezone.utc).isoformat()
    manifest = {"last_sync": now, "files": files_dict}

    for root in (local_root, remote_root):
        root.mkdir(parents=True, exist_ok=True)
        with open(root / MANIFEST_NAME, "w") as f:
            json.dump(manifest, f, indent=2)


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------

def classify_changes(source_entries: dict, dest_entries: dict,
                     manifest: dict, source_root: Path, dest_root: Path) -> dict:
    """
    Classify files into copy/conflict/unchanged.

    No automatic deletions. If a file exists on one side but not the other
    and it was previously synced, it's a conflict — the user must choose.

    Uses size for fast screening, hashes only when sizes match but we need
    to confirm content identity.
    """
    synced = manifest.get("files", {})
    all_keys = set(source_entries) | set(dest_entries) | set(synced)
    result = {"copy": [], "conflict": [], "unchanged": []}

    hashed = 0  # track how many files we had to hash

    for key in sorted(all_keys):
        in_src = key in source_entries
        in_dst = key in dest_entries
        in_man = key in synced

        if in_src and in_dst:
            # Both exist
            same_size = source_entries[key]["size"] == dest_entries[key]["size"]

            if in_man:
                manifest_hash = synced[key].get("hash")

                # Size-based fast screening
                src_size_changed = (source_entries[key]["size"] != synced[key].get("size"))
                dst_size_changed = (dest_entries[key]["size"] != synced[key].get("size"))

                if src_size_changed and not dst_size_changed:
                    # Source size changed, dest size matches manifest.
                    # But dest content could still differ (same size, different content).
                    # Hash dest to be sure.
                    dst_hash = file_hash(dest_root / key)
                    hashed += 1
                    if dst_hash == manifest_hash:
                        result["copy"].append(key)
                    else:
                        # Both changed
                        result["conflict"].append((key, source_entries[key], dest_entries[key]))
                elif dst_size_changed and not src_size_changed:
                    # Dest size changed, source size matches manifest.
                    # Hash source to confirm it hasn't changed content.
                    src_hash = file_hash(source_root / key)
                    hashed += 1
                    if src_hash == manifest_hash:
                        result["conflict"].append((key, source_entries[key], dest_entries[key]))
                    else:
                        # Both changed
                        result["conflict"].append((key, source_entries[key], dest_entries[key]))
                elif src_size_changed and dst_size_changed:
                    # Both sizes differ from manifest — hash both
                    src_hash = file_hash(source_root / key)
                    dst_hash = file_hash(dest_root / key)
                    hashed += 2
                    if src_hash == dst_hash:
                        result["unchanged"].append(key)
                    else:
                        result["conflict"].append((key, source_entries[key], dest_entries[key]))
                else:
                    # Neither size changed. Hash both to check for
                    # same-size content changes on either side.
                    src_hash = file_hash(source_root / key)
                    hashed += 1
                    src_changed = (src_hash != manifest_hash)

                    if not src_changed and same_size:
                        # Source unchanged. Check dest.
                        dst_hash = file_hash(dest_root / key)
                        hashed += 1
                        dst_changed = (dst_hash != manifest_hash)
                        if not dst_changed:
                            result["unchanged"].append(key)
                        else:
                            # Dest changed, source didn't
                            result["conflict"].append((key, source_entries[key], dest_entries[key]))
                    elif src_changed:
                        # Source changed. Check dest.
                        dst_hash = file_hash(dest_root / key)
                        hashed += 1
                        dst_changed = (dst_hash != manifest_hash)
                        if not dst_changed:
                            result["copy"].append(key)
                        elif src_hash == dst_hash:
                            # Both changed to the same content
                            result["unchanged"].append(key)
                        else:
                            result["conflict"].append((key, source_entries[key], dest_entries[key]))
            else:
                # No manifest (first sync) — hash both to check
                if same_size:
                    src_hash = file_hash(source_root / key)
                    dst_hash = file_hash(dest_root / key)
                    hashed += 2
                    if src_hash == dst_hash:
                        result["unchanged"].append(key)
                    else:
                        result["conflict"].append((key, source_entries[key], dest_entries[key]))
                else:
                    result["conflict"].append((key, source_entries[key], dest_entries[key]))

        elif in_src and not in_dst:
            if in_man:
                # Was synced, now missing from dest.
                # NEVER auto-delete. Ask the user.
                result["conflict"].append((key, source_entries[key], None))
            else:
                # New on source — copy to dest
                result["copy"].append(key)

        elif not in_src and in_dst:
            if in_man:
                # Was synced, now missing from source.
                # NEVER auto-delete. Ask the user.
                result["conflict"].append((key, None, dest_entries[key]))
            else:
                # Only on dest, not in source — leave alone
                result["unchanged"].append(key)

    if hashed > 0:
        print(f"  Hashed {hashed} files for content comparison")

    return result


def classify_bidirectional(local_entries: dict, remote_entries: dict,
                           manifest: dict, local_root: Path,
                           remote_root: Path) -> dict:
    """
    Classify files for bidirectional sync.

    Returns {
        'copy_to_remote': [keys],   # local changed, remote didn't
        'copy_to_local': [keys],    # remote changed, local didn't
        'conflict': [(key, local_info, remote_info)],
        'unchanged': [keys],
    }

    Same safety guarantees as classify_changes: no automatic deletions,
    missing files become conflicts.
    """
    synced = manifest.get("files", {})
    all_keys = set(local_entries) | set(remote_entries) | set(synced)
    result = {"copy_to_remote": [], "copy_to_local": [], "conflict": [],
              "unchanged": []}

    hashed = 0

    def get_hash(root, key):
        nonlocal hashed
        hashed += 1
        return file_hash(root / key)

    def changed_from_manifest(root, key, entries):
        """Check if file content differs from manifest. Returns (changed, hash)."""
        manifest_hash = synced[key].get("hash")
        size_changed = (entries[key]["size"] != synced[key].get("size"))
        if size_changed:
            return True, None
        h = get_hash(root, key)
        return (h != manifest_hash), h

    for key in sorted(all_keys):
        in_local = key in local_entries
        in_remote = key in remote_entries
        in_man = key in synced

        if in_local and in_remote:
            same_size = local_entries[key]["size"] == remote_entries[key]["size"]

            if in_man:
                local_changed, _ = changed_from_manifest(
                    local_root, key, local_entries)
                remote_changed, _ = changed_from_manifest(
                    remote_root, key, remote_entries)

                if local_changed and not remote_changed:
                    result["copy_to_remote"].append(key)
                elif remote_changed and not local_changed:
                    result["copy_to_local"].append(key)
                elif local_changed and remote_changed:
                    # Both changed — check if they changed to the same thing
                    local_h = get_hash(local_root, key)
                    remote_h = get_hash(remote_root, key)
                    if local_h == remote_h:
                        result["unchanged"].append(key)
                    else:
                        result["conflict"].append(
                            (key, local_entries[key], remote_entries[key]))
                else:
                    result["unchanged"].append(key)
            else:
                # No manifest — first sync
                if same_size:
                    local_h = get_hash(local_root, key)
                    remote_h = get_hash(remote_root, key)
                    if local_h == remote_h:
                        result["unchanged"].append(key)
                    else:
                        result["conflict"].append(
                            (key, local_entries[key], remote_entries[key]))
                else:
                    result["conflict"].append(
                        (key, local_entries[key], remote_entries[key]))

        elif in_local and not in_remote:
            if in_man:
                # Was synced, now missing from remote
                result["conflict"].append((key, local_entries[key], None))
            else:
                # New locally — copy to remote
                result["copy_to_remote"].append(key)

        elif not in_local and in_remote:
            if in_man:
                # Was synced, now missing from local
                result["conflict"].append((key, None, remote_entries[key]))
            else:
                # New on remote — copy to local
                result["copy_to_local"].append(key)

    if hashed > 0:
        print(f"  Hashed {hashed} files for content comparison")

    return result


# ---------------------------------------------------------------------------
# Display and conflict resolution
# ---------------------------------------------------------------------------

def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


def print_plan(changes: dict, direction: str, source_root: Path, dest_root: Path) -> bool:
    copies = changes["copy"]
    conflicts = changes["conflict"]
    unchanged = changes["unchanged"]

    print(f"\n{'=' * 60}")
    print(f"  {direction}")
    print(f"  {source_root}")
    print(f"    -> {dest_root}")
    print(f"{'=' * 60}\n")

    if not copies and not conflicts:
        print("  Everything is in sync. Nothing to do.\n")
        return False

    if copies:
        print(f"  COPY ({len(copies)} files):")
        for key in copies[:50]:
            print(f"    + {key}")
        if len(copies) > 50:
            print(f"    ... and {len(copies) - 50} more")
        print()

    if conflicts:
        print(f"  CONFLICTS ({len(conflicts)} files — requires your input):")
        for item in conflicts:
            key = item[0]
            src_info = item[1]
            dst_info = item[2]
            src_label = format_size(src_info["size"]) if src_info else "MISSING"
            dst_label = format_size(dst_info["size"]) if dst_info else "MISSING"
            print(f"    ? {key}  [source: {src_label}, dest: {dst_label}]")
        print()

    print(f"  Unchanged: {len(unchanged)} files")
    print()
    return True


def resolve_conflicts(conflicts: list, source_label: str, dest_label: str,
                      direction: str) -> list:
    """
    Ask user to resolve each conflict.
    Returns list of (key, action).

    Actions:
      'copy_to_dest'   — copy source file to dest (overwrite or create)
      'copy_to_source' — copy dest file to source (overwrite or create)
      'delete_dest'    — delete file from dest
      'delete_source'  — delete file from source
      'skip'           — do nothing

    Deletion is never the default and always requires typing the full
    word 'delete'.
    """
    resolutions = []
    print(f"\n--- Resolving {len(conflicts)} conflicts ---\n")

    for i, (key, src_info, dst_info) in enumerate(conflicts, 1):
        print(f"  [{i}/{len(conflicts)}] {key}")

        if src_info is not None and dst_info is not None:
            # Both exist but differ — offer to keep one side's version
            print(f"    {source_label}: {format_size(src_info['size'])}")
            print(f"    {dest_label}: {format_size(dst_info['size'])}")
            print(f"    [s] keep {source_label} version (overwrite {dest_label})")
            print(f"    [d] keep {dest_label} version (overwrite {source_label})")
            print(f"    [k] skip (do nothing)")

            while True:
                choice = input("    Choice: ").strip().lower()
                if choice in ("s", "source"):
                    resolutions.append((key, "use_source"))
                    break
                elif choice in ("d", "dest"):
                    resolutions.append((key, "use_dest"))
                    break
                elif choice in ("k", "skip"):
                    resolutions.append((key, "skip"))
                    break
                else:
                    print("    Please enter s, d, or k.")

        elif src_info is not None and dst_info is None:
            # File exists on source, missing from dest (was previously synced)
            print(f"    {source_label}: {format_size(src_info['size'])}")
            print(f"    {dest_label}: MISSING (was previously synced)")
            print(f"    [c] copy to {dest_label} (restore it)")
            print(f"    [k] skip (do nothing)")
            print(f"    Type 'delete' to remove from {source_label}")

            while True:
                choice = input("    Choice: ").strip().lower()
                if choice in ("c", "copy"):
                    resolutions.append((key, "copy_to_dest"))
                    break
                elif choice in ("k", "skip"):
                    resolutions.append((key, "skip"))
                    break
                elif choice == "delete":
                    resolutions.append((key, "delete_source"))
                    break
                else:
                    print("    Please enter c, k, or 'delete'.")

        elif src_info is None and dst_info is not None:
            # File exists on dest, missing from source (was previously synced)
            print(f"    {source_label}: MISSING (was previously synced)")
            print(f"    {dest_label}: {format_size(dst_info['size'])}")
            print(f"    [c] copy to {source_label} (restore it)")
            print(f"    [k] skip (do nothing)")
            print(f"    Type 'delete' to remove from {dest_label}")

            while True:
                choice = input("    Choice: ").strip().lower()
                if choice in ("c", "copy"):
                    resolutions.append((key, "copy_to_source"))
                    break
                elif choice in ("k", "skip"):
                    resolutions.append((key, "skip"))
                    break
                elif choice == "delete":
                    resolutions.append((key, "delete_dest"))
                    break
                else:
                    print("    Please enter c, k, or 'delete'.")
        else:
            continue

        print()

    return resolutions


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def copy_file(src_root: Path, dst_root: Path, key: str):
    src = src_root / key
    dst = dst_root / key
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def delete_file(root: Path, key: str):
    path = root / key
    if path.exists():
        path.unlink()
        parent = path.parent
        while parent != root:
            try:
                parent.rmdir()
                parent = parent.parent
            except OSError:
                break


def execute_sync(changes: dict, resolutions: list,
                 source_root: Path, dest_root: Path) -> tuple[int, int, int]:
    """
    Execute sync operations.

    Safety: copies go source->dest only. Deletions only happen through
    explicit conflict resolution. Push never touches local files except
    through user-resolved conflicts.
    """
    copied = 0
    deleted = 0
    resolved = 0

    # Copy new/modified files: always source -> dest
    total_copies = len(changes["copy"])
    if total_copies:
        print(f"Copying {total_copies} files...")
    for i, key in enumerate(changes["copy"], 1):
        print(f"  [{i}/{total_copies}] {key}")
        copy_file(source_root, dest_root, key)
        copied += 1

    # Conflict resolutions
    for key, action in resolutions:
        if action == "skip":
            continue
        elif action == "use_source":
            copy_file(source_root, dest_root, key)
        elif action == "use_dest":
            copy_file(dest_root, source_root, key)
        elif action == "copy_to_dest":
            copy_file(source_root, dest_root, key)
        elif action == "copy_to_source":
            copy_file(dest_root, source_root, key)
        elif action == "delete_source":
            delete_file(source_root, key)
            deleted += 1
        elif action == "delete_dest":
            delete_file(dest_root, key)
            deleted += 1
        resolved += 1

    return copied, deleted, resolved


# ---------------------------------------------------------------------------
# Build post-sync manifest
# ---------------------------------------------------------------------------

def build_manifest_files(local_root: Path, remote_root: Path,
                         dir_patterns: list, file_patterns: list) -> dict:
    """
    Build the manifest 'files' dict by scanning both sides and hashing
    every file. Only called after sync when the file count is small
    (post-copy, post-resolution).
    """
    local_entries = scan_tree(local_root, dir_patterns, file_patterns)
    remote_entries = scan_tree(remote_root, dir_patterns, file_patterns)
    all_keys = set(local_entries) | set(remote_entries)

    files = {}
    sorted_keys = sorted(all_keys)
    total = len(sorted_keys)
    for i, key in enumerate(sorted_keys, 1):
        if i == 1 or i % 100 == 0 or i == total:
            print(f"  Hashing [{i}/{total}]...")
        # Prefer local for hash (it's the primary copy)
        if key in local_entries:
            h = file_hash(local_root / key)
            files[key] = {"hash": h, "size": local_entries[key]["size"]}
        elif key in remote_entries:
            h = file_hash(remote_root / key)
            files[key] = {"hash": h, "size": remote_entries[key]["size"]}

    return files


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_sync(direction: str, auto_yes: bool = False):
    local_root, remote_root = load_config()

    if direction == "push":
        source_root, dest_root = local_root, remote_root
        source_label, dest_label = "local", "dropbox"
        direction_display = "PUSH: local -> Dropbox"
    else:
        source_root, dest_root = remote_root, local_root
        source_label, dest_label = "dropbox", "local"
        direction_display = "PULL: Dropbox -> local"

    dest_root.mkdir(parents=True, exist_ok=True)
    dir_patterns, file_patterns = load_syncignore(local_root)

    print("Scanning files...")
    source_entries = scan_tree(source_root, dir_patterns, file_patterns)
    dest_entries = scan_tree(dest_root, dir_patterns, file_patterns)
    manifest = load_manifest(local_root)

    print(f"  Source ({source_label}): {len(source_entries)} files")
    print(f"  Dest ({dest_label}):   {len(dest_entries)} files")

    changes = classify_changes(
        source_entries, dest_entries, manifest, source_root, dest_root
    )
    has_work = print_plan(changes, direction_display, source_root, dest_root)

    if not has_work:
        return

    resolutions = []
    if changes["conflict"]:
        if auto_yes:
            print("CONFLICTS require interactive resolution. Cannot use --yes.")
            print("Run again without --yes to resolve conflicts.")
            return
        resolutions = resolve_conflicts(
            changes["conflict"], source_label, dest_label, direction
        )

    if not auto_yes:
        proceed = input("Proceed with sync? [y/n] ").strip().lower()
        if proceed not in ("y", "yes"):
            print("Cancelled.")
            return

    copied, deleted, resolved = execute_sync(
        changes, resolutions, source_root, dest_root
    )

    # Build manifest by hashing all files on both sides
    print("Updating manifest...")
    manifest_files = build_manifest_files(
        local_root, remote_root, dir_patterns, file_patterns
    )
    save_manifests(manifest_files, local_root, remote_root)

    print(f"\nDone. Copied: {copied}, Deleted: {deleted}, Conflicts resolved: {resolved}")


def cmd_status():
    local_root, remote_root = load_config()
    dir_patterns, file_patterns = load_syncignore(local_root)

    print("Scanning files...")
    local_entries = scan_tree(local_root, dir_patterns, file_patterns)
    remote_entries = scan_tree(remote_root, dir_patterns, file_patterns)
    manifest = load_manifest(local_root)

    synced = manifest.get("files", {})
    last_sync = manifest.get("last_sync", "never")

    print(f"\n  Last sync: {last_sync}")
    print(f"  Local:   {len(local_entries)} files")
    print(f"  Dropbox: {len(remote_entries)} files")
    print()

    def report_changes(entries, side_label, root):
        new, modified, deleted = [], [], []
        all_keys = sorted(set(entries) | set(synced))
        for key in all_keys:
            in_entries = key in entries
            in_synced = key in synced
            if in_entries and not in_synced:
                new.append(key)
            elif in_entries and in_synced:
                if entries[key]["size"] != synced[key].get("size"):
                    modified.append(key)
                # Size matches — could still differ in content, but we don't
                # hash on status (too slow). Push/pull will catch it.
            elif not in_entries and in_synced:
                deleted.append(key)

        total = len(new) + len(modified) + len(deleted)
        if total:
            print(f"  {side_label} changes ({total} files since last sync):")
            for k in new[:20]:
                print(f"    + {k}")
            if len(new) > 20:
                print(f"    ... and {len(new) - 20} more new files")
            for k in modified[:20]:
                print(f"    ~ {k}")
            if len(modified) > 20:
                print(f"    ... and {len(modified) - 20} more modified files")
            for k in deleted[:20]:
                print(f"    - {k}")
            if len(deleted) > 20:
                print(f"    ... and {len(deleted) - 20} more deleted files")
            print()
        else:
            print(f"  No {side_label} changes since last sync.\n")

    report_changes(local_entries, "Local", local_root)
    report_changes(remote_entries, "Dropbox", remote_root)


def cmd_sync_workspace(auto_yes: bool = False):
    """Bidirectional sync: local <-> Dropbox in one step."""
    local_root, remote_root = load_config()
    remote_root.mkdir(parents=True, exist_ok=True)
    dir_patterns, file_patterns = load_syncignore(local_root)

    print("Scanning files...")
    local_entries = scan_tree(local_root, dir_patterns, file_patterns)
    remote_entries = scan_tree(remote_root, dir_patterns, file_patterns)
    manifest = load_manifest(local_root)

    print(f"  Local:   {len(local_entries)} files")
    print(f"  Dropbox: {len(remote_entries)} files")

    changes = classify_bidirectional(
        local_entries, remote_entries, manifest, local_root, remote_root
    )

    to_remote = changes["copy_to_remote"]
    to_local = changes["copy_to_local"]
    conflicts = changes["conflict"]
    unchanged = changes["unchanged"]

    print(f"\n{'=' * 60}")
    print(f"  SYNC: local <-> Dropbox")
    print(f"  {local_root}")
    print(f"  {remote_root}")
    print(f"{'=' * 60}\n")

    if not to_remote and not to_local and not conflicts:
        print("  Everything is in sync. Nothing to do.\n")
        return

    if to_remote:
        print(f"  LOCAL -> DROPBOX ({len(to_remote)} files):")
        for key in to_remote[:50]:
            print(f"    > {key}")
        if len(to_remote) > 50:
            print(f"    ... and {len(to_remote) - 50} more")
        print()

    if to_local:
        print(f"  DROPBOX -> LOCAL ({len(to_local)} files):")
        for key in to_local[:50]:
            print(f"    < {key}")
        if len(to_local) > 50:
            print(f"    ... and {len(to_local) - 50} more")
        print()

    if conflicts:
        print(f"  CONFLICTS ({len(conflicts)} files — requires your input):")
        for item in conflicts:
            key = item[0]
            local_info = item[1]
            remote_info = item[2]
            local_label = format_size(local_info["size"]) if local_info else "MISSING"
            remote_label = format_size(remote_info["size"]) if remote_info else "MISSING"
            print(f"    ? {key}  [local: {local_label}, dropbox: {remote_label}]")
        print()

    print(f"  Unchanged: {len(unchanged)} files")
    print()

    # Resolve conflicts
    resolutions = []
    if conflicts:
        if auto_yes:
            print("CONFLICTS require interactive resolution. Cannot use --yes.")
            print("Run again without --yes to resolve conflicts.")
            return
        resolutions = resolve_conflicts(conflicts, "local", "dropbox", "sync")

    if not auto_yes:
        proceed = input("Proceed with sync? [y/n] ").strip().lower()
        if proceed not in ("y", "yes"):
            print("Cancelled.")
            return

    # Execute copies
    copied = 0
    deleted = 0
    total_to_remote = len(to_remote)
    total_to_local = len(to_local)

    if total_to_remote:
        print(f"Copying {total_to_remote} files local -> Dropbox...")
    for i, key in enumerate(to_remote, 1):
        print(f"  [{i}/{total_to_remote}] {key}")
        copy_file(local_root, remote_root, key)
        copied += 1

    if total_to_local:
        print(f"Copying {total_to_local} files Dropbox -> local...")
    for i, key in enumerate(to_local, 1):
        print(f"  [{i}/{total_to_local}] {key}")
        copy_file(remote_root, local_root, key)
        copied += 1

    # Execute conflict resolutions
    resolved = 0
    for key, action in resolutions:
        if action == "skip":
            continue
        elif action == "use_source":
            copy_file(local_root, remote_root, key)
        elif action == "use_dest":
            copy_file(remote_root, local_root, key)
        elif action == "copy_to_dest":
            copy_file(local_root, remote_root, key)
        elif action == "copy_to_source":
            copy_file(remote_root, local_root, key)
        elif action == "delete_source":
            delete_file(local_root, key)
            deleted += 1
        elif action == "delete_dest":
            delete_file(remote_root, key)
            deleted += 1
        resolved += 1

    # Build manifest
    print("Updating manifest...")
    manifest_files = build_manifest_files(
        local_root, remote_root, dir_patterns, file_patterns
    )
    save_manifests(manifest_files, local_root, remote_root)

    print(f"\nDone. Copied: {copied}, Deleted: {deleted}, Conflicts resolved: {resolved}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python workspace_sync.py [init|sync-workspace|push-workspace|pull-workspace|status] [--yes]")
        sys.exit(1)

    cmd = sys.argv[1]
    auto_yes = "--yes" in sys.argv or "-y" in sys.argv
    if cmd == "sync-workspace":
        cmd_sync_workspace(auto_yes=auto_yes)
    elif cmd == "push-workspace":
        cmd_sync("push", auto_yes=auto_yes)
    elif cmd == "pull-workspace":
        cmd_sync("pull", auto_yes=auto_yes)
    elif cmd == "status":
        cmd_status()
    elif cmd == "init":
        cmd_init()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python workspace_sync.py [init|sync-workspace|push-workspace|pull-workspace|status] [--yes]")
        sys.exit(1)


if __name__ == "__main__":
    main()
