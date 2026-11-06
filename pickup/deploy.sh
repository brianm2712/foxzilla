#!/usr/bin/env bash
# Deploy foxzilla-pickup from this git checkout to the Proxmox host.
#
#   pickup/deploy.sh [--dry-run] [--no-wait] [--wait-timeout SECONDS]
#
# What it does (run from the Mac, from any directory):
#   1. refuses if pickup/ has uncommitted changes; deploys HEAD
#   2. drift guard: every file in the host build dir must match the commit the
#      host is on (its VERSION, or the commit whose media_pickup.py matches)
#   3. ships `git archive HEAD pickup` to $BUILD_DIR.new, writes VERSION,
#      runs the tests there and builds foxzilla-pickup:<sha> (labelled too)
#   4. waits until the pickup is idle and holds its lock so no new tick starts
#      (skip with --no-wait: an in-flight handoff is then killed and restarts;
#      gives up after --wait-timeout seconds, default 6 h)
#   5. moves the old build dir aside to $BUILD_DIR.prev-<sha|pre-git>-<time>
#      (never deleted), swaps $BUILD_DIR.new into place, points :latest at the
#      new image, recreates the container with run.sh and checks it is Up on
#      the new SHA
#   6. installs the logrotate config and validates it with `logrotate -d`
#
# --dry-run does 1-2 for real (read-only ssh) and prints the remote commands
# of 3-6 instead of running them.
#
# Nothing on the host is ever deleted by this script except its own
# $BUILD_DIR.new staging dir (recognised by the .deploy-new marker inside it).
set -euo pipefail

HOST="${HOST:-100.102.227.73}"
KEY="${KEY:-$HOME/.ssh/proxmox}"
BUILD_DIR=/root/foxzilla-build
CONTAINER=foxzilla-pickup
IMAGE=foxzilla-pickup:latest
HOST_LOG=/srv/foxzilla-config/pickup.log
LOCK_FILE=/tmp/media_pickup.lock
GATE_FILE=/tmp/deploy-gate
GATE_PID_FILE=/tmp/deploy-gate.pid
GATE_SECONDS=900
POLL_SECONDS="${POLL_SECONDS:-30}"
NEW_MARKER=.deploy-new

DRY_RUN=0
WAIT=1
WAIT_TIMEOUT=21600

say()  { echo "deploy: $*"; }
warn() { echo "deploy: WARNING: $*" >&2; }
die()  { echo "deploy: ERROR: $*" >&2; exit 1; }

usage() { echo "usage: $0 [--dry-run] [--no-wait] [--wait-timeout SECONDS]" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-wait) WAIT=0 ;;
        --wait-timeout)
            [ "$#" -ge 2 ] || usage
            WAIT_TIMEOUT="$2"; shift ;;
        --wait-timeout=*) WAIT_TIMEOUT="${1#*=}" ;;
        -h|--help) usage ;;
        *) echo "deploy: unknown option: $1" >&2; usage ;;
    esac
    shift
done
case "$WAIT_TIMEOUT" in
    ''|*[!0-9]*) echo "deploy: --wait-timeout needs a whole number of seconds" >&2; usage ;;
esac

remote() { ssh -i "$KEY" -o ConnectTimeout=10 "root@$HOST" "$@"; }

# A mutating remote command: printed under --dry-run, run otherwise.
mutate() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'deploy: [dry-run] ssh root@%s %s\n' "$HOST" "$1"
    else
        remote "$1"
    fi
}

md5_local() {
    if command -v md5 >/dev/null 2>&1; then md5 -q; else md5sum | cut -d' ' -f1; fi
}

# md5 of every regular file under the current dir, as `<md5>  ./<path>`
# sorted by path - the same shape `md5sum` prints on the host.
md5_tree_local() {
    if command -v md5 >/dev/null 2>&1; then
        find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name VERSION -exec md5 -r {} +
    else
        find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name VERSION -exec md5sum {} +
    fi | sed -E 's/^([0-9a-f]{32}) +/\1  /' | LC_ALL=C sort -k2
}

# Python run inside the container. Double quotes only: these are embedded in
# single quotes on the remote command line. The gate carries a marker comment
# so the release script and the liveness poll can find it; both build the
# marker by concatenation so they never match themselves. The gate writes its
# pid first, then blocks on the pickup's lock, then announces it holds it.
GATE_PY="import fcntl,os,time; open(\"$GATE_PID_FILE\",\"w\").write(str(os.getpid())); f=open(\"$LOCK_FILE\",\"a\"); fcntl.flock(f,fcntl.LOCK_EX); open(\"$GATE_FILE\",\"w\").write(str(os.getpid())); time.sleep($GATE_SECONDS)  # DEPLOY_GATE"
RELEASE_PY="import os,signal
m=(\"DEPLOY\"+\"_GATE\").encode()
for p in [d for d in os.listdir(\"/proc\") if d.isdigit()]:
    try:
        if int(p)!=os.getpid() and m in open(\"/proc/\"+p+\"/cmdline\",\"rb\").read(): os.kill(int(p),signal.SIGKILL)
    except Exception: pass
for f in (\"$GATE_FILE\",\"$GATE_PID_FILE\"):
    try: os.remove(f)
    except FileNotFoundError: pass"
# One poll of the gate, run by sh in the container: held | waiting | nopid | dead.
# A dead gate (or a zombie, whose cmdline is empty, or a reused pid) has no
# marker in /proc/<pid>/cmdline.
GATE_POLL_SH="if [ -f $GATE_FILE ]; then echo held; elif [ ! -s $GATE_PID_FILE ]; then echo nopid; elif grep -qa DEPLOY\"\"_GATE /proc/\$(cat $GATE_PID_FILE)/cmdline 2>/dev/null; then echo waiting; else echo dead; fi"

LOCAL_TMP=""
GATE_HELD=0
SWAPPED=0
PREV=""
OLD_IMAGE=""
# shellcheck disable=SC2329  # invoked via trap
on_exit() {
    rc=$?
    if [ -n "$LOCAL_TMP" ]; then rm -rf "$LOCAL_TMP"; fi
    if [ "$rc" -ne 0 ] && [ "$GATE_HELD" -eq 1 ]; then
        warn "failed while holding the deploy gate; releasing it so the pickup resumes"
        remote "docker exec $CONTAINER python3 -c '$RELEASE_PY'" || \
            warn "could not release the gate; it expires on its own within ${GATE_SECONDS}s"
    fi
    if [ "$rc" -ne 0 ] && [ "$SWAPPED" -eq 1 ]; then
        warn "failed during or after the swap; $BUILD_DIR may now hold the new build"
        if [ -n "$PREV" ]; then
            warn "previous build dir: $PREV"
        fi
        if [ -n "$OLD_IMAGE" ] && [ -n "$PREV" ]; then
            warn "roll back: ssh root@$HOST 'docker tag $OLD_IMAGE $IMAGE && cd $PREV && sh ./run.sh'"
        else
            warn "no previous container image/build dir recorded; nothing to roll back to"
        fi
    fi
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- 1. repo + what is being deployed --------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)" || die "not inside a git checkout"
cd "$REPO_ROOT"

[ -d pickup ] || die "no pickup/ directory in $REPO_ROOT"
if [ -n "$(git status --porcelain -- pickup)" ]; then
    git status --short -- pickup >&2
    die "uncommitted changes under pickup/ - commit them first (deploys ship HEAD, not the working tree)"
fi

SHA="$(git rev-parse HEAD)"
SHA_IMAGE="foxzilla-pickup:$SHA"
STAMP="$(date +%Y%m%d-%H%M%S)"
say "deploying $(git log -1 --format='%h %s' HEAD)"
say "full SHA $SHA"
[ "$DRY_RUN" -eq 1 ] && say "DRY RUN: steps 3-6 are printed, not run"
# Deliberately a warning, not a refusal: the owner decides when to push, and
# deploying an unpushed branch is allowed - it is just called out loudly.
if [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
    warn "HEAD $SHA is not on any remote branch (unpushed). Deploying it anyway."
fi

# --- 2. drift guard ---------------------------------------------------------
# The host build dir was hand-maintained for a long time, so check every file
# in it - not just media_pickup.py - against the commit it claims to be.
say "checking what the host is running ($HOST:$BUILD_DIR)"
remote true || die "cannot ssh to root@$HOST with $KEY (tailnet up? sandbox off?)"
HOST_VERSION="$(remote "cat $BUILD_DIR/VERSION 2>/dev/null || true" | tr -d '[:space:]')"
HOST_SUMS="$(remote "if [ -d $BUILD_DIR ]; then cd $BUILD_DIR && find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name VERSION -exec md5sum {} + ; fi" \
    | LC_ALL=C sort -k2)" || die "could not list $BUILD_DIR on the host"

BASE=""
if [ -n "$HOST_VERSION" ]; then
    git cat-file -e "${HOST_VERSION}^{commit}" 2>/dev/null || \
        die "host runs commit $HOST_VERSION which you don't have locally - fetch/pull first, someone else deployed"
    BASE="$HOST_VERSION"
    PREV_TAG="$(git rev-parse --short "$HOST_VERSION")"
    say "host is on $(git log -1 --format='%h %s' "$HOST_VERSION")"
    if [ "$HOST_VERSION" != "$SHA" ] && ! git merge-base --is-ancestor "$HOST_VERSION" HEAD; then
        warn "HEAD does not contain the host's commit ${HOST_VERSION:0:12}: this deploy drops whatever that commit added"
    fi
else
    PREV_TAG=pre-git
    HOST_MD5="$(printf '%s\n' "$HOST_SUMS" | awk '$2 == "./media_pickup.py" { print $1 }')"
    if [ -z "$HOST_MD5" ]; then
        [ -z "$HOST_SUMS" ] || {
            printf '%s\n' "$HOST_SUMS" | sed 's/^/    /' >&2
            die "$BUILD_DIR has files but no VERSION and no media_pickup.py - can't tell what it is; move it aside first"
        }
        warn "no VERSION and no files in $BUILD_DIR on the host: treating as a fresh install"
    else
        while read -r c; do
            if [ "$(git show "$c:pickup/media_pickup.py" 2>/dev/null | md5_local)" = "$HOST_MD5" ]; then
                BASE="$c"; break
            fi
        done < <(git log --format=%H -- pickup/media_pickup.py)
        [ -n "$BASE" ] || die "host's media_pickup.py (md5 $HOST_MD5) matches no commit - host has changes not in git; get them into git first"
        say "no VERSION on host (first scripted deploy); its media_pickup.py matches $(git log -1 --format='%h %s' "$BASE")"
    fi
fi

if [ -n "$BASE" ]; then
    LOCAL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/foxzilla-deploy.XXXXXX")"
    git archive --format=tar "$BASE" pickup | tar -x -C "$LOCAL_TMP"
    GIT_SUMS="$( (cd "$LOCAL_TMP/pickup" && md5_tree_local) )"
    printf '%s\n' "$HOST_SUMS" | cut -c35- | LC_ALL=C sort > "$LOCAL_TMP/host.paths"
    printf '%s\n' "$GIT_SUMS"  | cut -c35- | LC_ALL=C sort > "$LOCAL_TMP/git.paths"
    printf '%s\n' "$HOST_SUMS" | LC_ALL=C sort > "$LOCAL_TMP/host.sums"
    printf '%s\n' "$GIT_SUMS"  | LC_ALL=C sort > "$LOCAL_TMP/git.sums"
    ONLY_HOST="$(LC_ALL=C comm -23 "$LOCAL_TMP/host.paths" "$LOCAL_TMP/git.paths" | sed '/^$/d')"
    ONLY_GIT="$(LC_ALL=C comm -13 "$LOCAL_TMP/host.paths" "$LOCAL_TMP/git.paths" | sed '/^$/d')"
    # paths in both whose host line (md5 + path) has no identical git line
    DIFFERS="$(LC_ALL=C comm -23 "$LOCAL_TMP/host.sums" "$LOCAL_TMP/git.sums" | cut -c35- \
        | LC_ALL=C comm -12 - "$LOCAL_TMP/git.paths" | sed '/^$/d')"
    BASE_DESC="$(git log -1 --format='%h' "$BASE")"
    if [ -n "$ONLY_GIT" ]; then
        say "note: $BASE_DESC has files the host build dir lacks (host predates them):"
        printf '%s\n' "$ONLY_GIT" | sed 's/^/deploy:     /'
    fi
    if [ -n "$ONLY_HOST" ] || [ -n "$DIFFERS" ]; then
        if [ -n "$ONLY_HOST" ]; then
            echo "deploy: files in $BUILD_DIR that are not in $BASE_DESC:" >&2
            printf '%s\n' "$ONLY_HOST" | sed 's/^/deploy:     /' >&2
        fi
        if [ -n "$DIFFERS" ]; then
            echo "deploy: files in $BUILD_DIR that differ from $BASE_DESC:" >&2
            printf '%s\n' "$DIFFERS" | sed 's/^/deploy:     /' >&2
        fi
        die "host build dir has drifted from git - commit those changes, or move the files aside on the host, then retry"
    fi
    say "every file in the host build dir matches $BASE_DESC"
fi

NEW="${BUILD_DIR}.new"
PREV="${BUILD_DIR}.prev-${PREV_TAG}-${STAMP}"
STATE="$(remote "if [ -e $NEW ] && [ ! -f $NEW/$NEW_MARKER ]; then echo unexpected-new; fi; if [ -e $PREV ]; then echo prev-exists; fi; if [ ! -e $BUILD_DIR ]; then echo no-build-dir; fi")"
case "$STATE" in *unexpected-new*) die "unexpected $NEW (no $NEW_MARKER marker - not made by this script), inspect it" ;; esac
case "$STATE" in *prev-exists*) die "$PREV already exists; retry in a second" ;; esac
case "$STATE" in *no-build-dir*) PREV="" ;; esac

OLD_IMAGE="$(remote "docker inspect -f '{{.Image}}' $CONTAINER 2>/dev/null || true")"
if [ -n "$OLD_IMAGE" ]; then
    say "current container image: $OLD_IMAGE"
else
    warn "no $CONTAINER container on the host; nothing to roll back to"
fi

# --- 3. ship, test, build ---------------------------------------------------
say "shipping HEAD:pickup to $NEW"
mutate "set -e; if [ -e $NEW ]; then [ -f $NEW/$NEW_MARKER ] || { echo 'unexpected $NEW, inspect it' >&2; exit 1; }; rm -rf $NEW; fi; mkdir -p $NEW && touch $NEW/$NEW_MARKER"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'deploy: [dry-run] git archive --format=tar HEAD pickup | ssh root@%s %s\n' \
        "$HOST" "tar -x --strip-components=1 -C $NEW"
else
    git archive --format=tar HEAD pickup | remote "tar -x --strip-components=1 -C $NEW"
fi
mutate "echo $SHA > $NEW/VERSION"

say "running tests in $NEW"
for t in tests/test_pickup.py tests/test_credentials.py; do
    cmd="cd $NEW && PYTHONDONTWRITEBYTECODE=1 python3 $t 2>&1 | tail -1"
    if [ "$DRY_RUN" -eq 1 ]; then
        mutate "$cmd"
    else
        RESULT="$(remote "$cmd")"
        say "$t: $RESULT"
        case "$RESULT" in
            *" passed, 0 failed") ;;
            *) die "$t did not pass on the host (left in $NEW for inspection); nothing deployed" ;;
        esac
    fi
done

say "building $SHA_IMAGE"
mutate "docker build -q -t $SHA_IMAGE --label org.opencontainers.image.revision=$SHA $NEW"

# --- 4. wait for idle and hold the gate ------------------------------------
if [ "$WAIT" -eq 0 ]; then
    warn "--no-wait: recreating the container now; an in-flight handoff will restart from scratch"
elif [ "$(remote "docker inspect -f '{{.State.Running}}' $CONTAINER 2>/dev/null || true")" != "true" ]; then
    say "$CONTAINER is not running; no need to wait"
else
    say "clearing any stale deploy gate, then waiting for the pickup to go idle"
    mutate "docker exec $CONTAINER python3 -c '$RELEASE_PY'"
    mutate "docker exec -d $CONTAINER python3 -c '$GATE_PY'"
    # Never in a dry run: an interrupted dry run must not run the release
    # script against a real deploy's gate.
    if [ "$DRY_RUN" -eq 0 ]; then GATE_HELD=1; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'deploy: [dry-run] every %ss (up to %ss) until it prints held: ssh root@%s %s\n' \
            "$POLL_SECONDS" "$WAIT_TIMEOUT" "$HOST" "docker exec $CONTAINER sh -c '$GATE_POLL_SH'"
    else
        WAIT_START="$(date +%s)"
        POLLS=0
        while :; do
            GATE="$(remote "docker exec $CONTAINER sh -c '$GATE_POLL_SH'" || true)"
            POLLS=$((POLLS + 1))
            case "$GATE" in
                held) break ;;
                waiting) ;;
                nopid) [ "$POLLS" -le 1 ] || die "the deploy gate never wrote $GATE_PID_FILE; it did not start" ;;
                dead) die "the deploy gate process died before it got the lock; nothing swapped" ;;
                *) die "could not poll the deploy gate (got '${GATE}'); is $CONTAINER still running?" ;;
            esac
            if [ $(( $(date +%s) - WAIT_START )) -ge "$WAIT_TIMEOUT" ]; then
                die "pickup still busy after ${WAIT_TIMEOUT}s (--wait-timeout); giving up, nothing swapped"
            fi
            say "pickup busy: $(remote "tail -1 $HOST_LOG 2>/dev/null" || true)"
            sleep "$POLL_SECONDS"
        done
        # The gate holds for GATE_SECONDS from this moment; the swap below
        # takes seconds, well inside that window.
        say "pickup idle; gate held (new ticks skip for up to ${GATE_SECONDS}s)"
    fi
fi

# --- 5. swap and recreate ---------------------------------------------------
if [ -n "$PREV" ]; then
    say "moving $BUILD_DIR aside to $PREV (kept), swapping in $NEW, tagging $IMAGE, running run.sh"
else
    say "installing $NEW as $BUILD_DIR (none there before), tagging $IMAGE, running run.sh"
fi
SWAPPED=1
SWAP="set -e; rm -f $NEW/$NEW_MARKER"
if [ -n "$PREV" ]; then
    SWAP="$SWAP; [ ! -e $PREV ] || { echo '$PREV exists' >&2; exit 1; }; mv $BUILD_DIR $PREV"
fi
SWAP="$SWAP; mv $NEW $BUILD_DIR; docker tag $SHA_IMAGE $IMAGE; cd $BUILD_DIR && sh ./run.sh"
mutate "$SWAP"
GATE_HELD=0   # the gate lived in the old container, which run.sh removed

if [ "$DRY_RUN" -eq 1 ]; then
    mutate "docker ps --filter name=^${CONTAINER}\$ --format '{{.Status}}'"
    mutate "docker inspect -f '{{index .Config.Labels \"org.opencontainers.image.revision\"}}' $CONTAINER"
else
    sleep 3
    STATUS="$(remote "docker ps --filter name=^${CONTAINER}\$ --format '{{.Status}}'")"
    case "$STATUS" in
        Up*) say "container: $STATUS" ;;
        *) die "container $CONTAINER is not Up (status: '${STATUS:-absent}'); check: ssh root@$HOST docker logs $CONTAINER" ;;
    esac
    REV="$(remote "docker inspect -f '{{index .Config.Labels \"org.opencontainers.image.revision\"}}' $CONTAINER")"
    [ "$REV" = "$SHA" ] || die "container reports revision '$REV', expected $SHA"
    say "container runs revision $SHA"
fi
SWAPPED=0

# --- 6. logrotate -----------------------------------------------------------
say "installing logrotate config to /etc/logrotate.d/foxzilla-pickup"
mutate "install -m 0644 $BUILD_DIR/logrotate.conf /etc/logrotate.d/foxzilla-pickup && logrotate -d /etc/logrotate.d/foxzilla-pickup"

if [ "$DRY_RUN" -eq 1 ]; then
    say "dry run complete - nothing was changed on the host"
else
    if [ -n "$PREV" ]; then
        say "previous build kept in $PREV; roll back: ssh root@$HOST 'docker tag ${OLD_IMAGE:-<old image>} $IMAGE && cd $PREV && sh ./run.sh'"
    fi
    say "done: $(git log -1 --format='%h %s' HEAD) is live"
fi
exit 0
