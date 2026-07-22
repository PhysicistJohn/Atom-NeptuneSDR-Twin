#!/bin/sh
# Shared guard for build caches whose generated files embed absolute paths.
#
# This file is sourced by build scripts.  Call guard_relocatable_cache with a
# fully managed cache directory, a schema identifier, a diagnostic label, and
# the cache-relative artifacts that must be discarded after a move.  Downloads
# and pristine source trees can be omitted from that list and are preserved.

guard_relocatable_cache() {
    cache=$1
    schema=$2
    label=$3
    shift 3

    case "$cache" in
        ''|/|*'
'*) printf '%s\n' "$label: refusing unsafe cache path: $cache" >&2; return 2 ;;
    esac
    case "$schema" in
        ''|*'
'*) printf '%s\n' "$label: invalid cache schema" >&2; return 2 ;;
    esac

    mkdir -p "$cache" || return
    physical_cache=$(CDPATH= cd -- "$cache" && pwd -P) || return
    stamp="$cache/.neptune-cache-location"
    current_schema=
    current_path=
    if [ -f "$stamp" ]; then
        current_schema=$(sed -n 's/^schema=//p' "$stamp" | sed -n '1p')
        current_path=$(sed -n 's/^path=//p' "$stamp" | sed -n '1p')
    fi

    sensitive_present=no
    for relative in "$@"; do
        case "$relative" in
            ''|.|/*|..|../*|*/../*|*/..)
                printf '%s\n' "$label: unsafe cache artifact: $relative" >&2
                return 2
                ;;
        esac
        if [ -e "$cache/$relative" ] || [ -L "$cache/$relative" ]; then
            sensitive_present=yes
        fi
    done

    CACHE_RELOCATION_ACTION=unchanged
    CACHE_RELOCATION_REASON=
    if [ "$current_schema" != "$schema" ] || [ "$current_path" != "$physical_cache" ]; then
        if [ -z "$current_schema$current_path" ]; then
            CACHE_RELOCATION_REASON=legacy
        elif [ "$current_schema" != "$schema" ]; then
            CACHE_RELOCATION_REASON=schema-change
        else
            CACHE_RELOCATION_REASON=relocated
        fi

        if [ "$sensitive_present" = yes ]; then
            printf '%s: invalidating %s path-sensitive cache at %s\n' \
                "$label" "$CACHE_RELOCATION_REASON" "$physical_cache" >&2
            for relative in "$@"; do
                rm -rf "$cache/$relative"
            done
            CACHE_RELOCATION_ACTION=invalidated
        else
            CACHE_RELOCATION_ACTION=initialized
        fi
    fi

    stamp_relocatable_cache "$cache" "$schema"
}

# Record a successfully prepared cache without re-running invalidation.  This
# is needed for tools such as Micromamba that require their destination prefix
# not to exist before creation.
stamp_relocatable_cache() {
    neptune_stamp_cache=$1
    neptune_stamp_schema=$2
    case "$neptune_stamp_cache" in
        ''|/|*'
'*) return 2 ;;
    esac
    case "$neptune_stamp_schema" in
        ''|*'
'*) return 2 ;;
    esac
    mkdir -p "$neptune_stamp_cache" || return
    neptune_stamp_physical=$(CDPATH= cd -- "$neptune_stamp_cache" && pwd -P) || return
    neptune_stamp_path="$neptune_stamp_cache/.neptune-cache-location"
    neptune_stamp_temporary="$neptune_stamp_path.tmp.$$"
    {
        printf 'schema=%s\n' "$neptune_stamp_schema"
        printf 'path=%s\n' "$neptune_stamp_physical"
    } >"$neptune_stamp_temporary"
    mv -f "$neptune_stamp_temporary" "$neptune_stamp_path"
}

# Return success when a cache directory has no payload beyond the location
# stamp written by guard_relocatable_cache.  The guard creates the directory
# even for a fresh or invalidated cache, so callers must not use mere path
# existence to distinguish an empty managed cache from an unknown toolchain.
cache_has_only_relocation_stamp() {
    neptune_cache_directory=$1
    [ -d "$neptune_cache_directory" ] || return 0
    for neptune_cache_entry in \
        "$neptune_cache_directory"/* \
        "$neptune_cache_directory"/.[!.]* \
        "$neptune_cache_directory"/..?*; do
        if [ ! -e "$neptune_cache_entry" ] && [ ! -L "$neptune_cache_entry" ]; then
            continue
        fi
        if [ "$neptune_cache_entry" = \
            "$neptune_cache_directory/.neptune-cache-location" ]; then
            continue
        fi
        return 1
    done
    return 0
}
