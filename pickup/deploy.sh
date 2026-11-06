#!/usr/bin/env bash
# Deploy foxzilla-pickup from this git checkout to the Proxmox host.
#
#   pickup/deploy.sh [--dry-run] [--no-wait]
#
# What it does (run from the Mac, from any directory):
#   1. refuses if pickup/ has uncommitted changes; deploys HEAD
#   2. drift guard: the host must be running something that is in git
#   3. ships `git archive HEAD pickup` to $BUILD_DIR.new, writes VERSION,
#      runs the tests there and builds the image (labelled with the SHA)
#   4. waits until the pickup is idle and holds its lock so no new tick starts
#      (skip with --no-wait: an in-flight handoff is then killed and restarts)
#   5. swaps $BUILD_DIR.new into place (old one kept as $BUILD_DIR.prev),
#      recreates the container with run.sh and checks it is Up on the new SHA
#   6. installs the logrotate config and validates it with `logrotate -d`
#
# --dry-run does 1-2 for real (read-only ssh) and prints the remote commands
# of 3-6 instead of running them.
set -euo pipefail

HOST="${HOST:-100.102.227.73}"
KEY="${KEY:-$HOME/.ssh/proxmox}"
BUILD_DIR=/root/foxzilla-build
CONTAINER=foxzilla-pickup
IMAGE=foxzilla-pickup:latest
HOST_LOG=/srv/foxzilla-config/pickup.log
LOCK_FILE=/tmp/media_pickup.lock
GATE_FILE=/tmp/deploy-gate
GATE_SECONDS=900
POLL_SECONDS=30

DRY_RUN=0
WAIT=1

say()  { echo "deploy: $*"; }
warn() { echo "deploy: WARNING: $*" >&2; }
die()  { echo "deploy: ERROR: $*" >&2; exit 1; }

usage() { echo "usage: $0 [--dry-run] [--no-wait]" >&2; exit 2; }

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --no-wait) WAIT=0 ;;
        -h|--help) usage ;;
        *) echo "deploy: unknown option: $arg" >&2; usage ;;
    esac
done

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

# Python run inside the container. Double quotes only: these are embedded in
# single quotes on the remote command line. The gate carries a marker comment
# so the release script can find it; the release script builds the marker by
# concatenation so it never matches itself.
GATE_PY="import fcntl,os,time; f=open(\"$LOCK_FILE\",\"a\"); fcntl.flock(f,fcntl.LOCK_EX); open(\"$GATE_FILE\",\"w\").write(str(os.getpid())); time.sleep($GATE_SECONDS)  # DEPLOY_GATE"
RELEASE_PY="import os,signal
m=(\"DEPLOY\"+\"_GATE\").encode()
for p in [d for d in os.listdir(\"/proc\") if d.isdigit()]:
    try:
        if int(p)!=os.getpid() and m in open(\"/proc/\"+p+\"/cmdline\",\"rb\").read(): os.kill(int(p),signal.SIGKILL)
    except Exception: pass
try: os.remove(\"$GATE_FILE\")
except FileNotFoundError: pass"

GATE_HELD=0
SWAPPED=0
# shellcheck disable=SC2329  # invoked via trap
on_exit() {
    rc=$?
    if [ "$rc" -ne 0 ] && [ "$GATE_HELD" -eq 1 ]; then
        warn "failed while holding the deploy gate; releasing it so the pickup resumes"
        remote "docker exec $CONTAINER python3 -c '$RELEASE_PY'" || \
            warn "could not release the gate; it expires on its own within ${GATE_SECONDS}s"
    fi
    if [ "$rc" -ne 0 ] && [ "$SWAPPED" -eq 1 ]; then
        warn "the build dir was swapped; previous build is in ${BUILD_DIR}.prev"
        warn "roll back: ssh root@$HOST 'rm -rf $BUILD_DIR && mv ${BUILD_DIR}.prev $BUILD_DIR && cd $BUILD_DIR && docker build -t $IMAGE . && sh ./run.sh'"
    fi
}
trap on_exit EXIT

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
say "deploying $(git log -1 --format='%h %s' HEAD)"
say "full SHA $SHA"
[ "$DRY_RUN" -eq 1 ] && say "DRY RUN: steps 3-6 are printed, not run"
if [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
    warn "HEAD $SHA is not on any remote branch (unpushed). Deploying it anyway."
fi

# --- 2. drift guard ---------------------------------------------------------
say "checking what the host is running ($HOST:$BUILD_DIR)"
remote true || die "cannot ssh to root@$HOST with $KEY (tailnet up? sandbox off?)"
HOST_VERSION="$(remote "cat $BUILD_DIR/VERSION 2>/dev/null || true" | tr -d '[:space:]')"
if [ -n "$HOST_VERSION" ]; then
    git cat-file -e "${HOST_VERSION}^{commit}" 2>/dev/null || \
        die "host runs commit $HOST_VERSION which you don't have locally - fetch/pull first, someone else deployed"
    say "host is on $(git log -1 --format='%h %s' "$HOST_VERSION")"
    if [ "$HOST_VERSION" != "$SHA" ] && ! git merge-base --is-ancestor "$HOST_VERSION" HEAD; then
        warn "HEAD does not contain the host's commit ${HOST_VERSION:0:12}: this deploy drops whatever that commit added"
    fi
else
    HOST_MD5="$(remote "md5sum $BUILD_DIR/media_pickup.py 2>/dev/null || true" | cut -d' ' -f1)"
    if [ -z "$HOST_MD5" ]; then
        warn "no VERSION and no $BUILD_DIR/media_pickup.py on the host: treating as a fresh install"
    else
        MATCH=""
        while read -r c; do
            if [ "$(git show "$c:pickup/media_pickup.py" 2>/dev/null | md5_local)" = "$HOST_MD5" ]; then
                MATCH="$c"; break
            fi
        done < <(git log --format=%H -- pickup/media_pickup.py)
        [ -n "$MATCH" ] || die "host's media_pickup.py (md5 $HOST_MD5) matches no commit - host has changes not in git; get them into git first"
        say "no VERSION on host (first scripted deploy); its media_pickup.py matches $(git log -1 --format='%h %s' "$MATCH")"
    fi
fi

# --- 3. ship, test, build ---------------------------------------------------
NEW="${BUILD_DIR}.new"
say "shipping HEAD:pickup to $NEW"
mutate "rm -rf $NEW && mkdir -p $NEW"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'deploy: [dry-run] git archive --format=tar HEAD pickup | ssh root@%s %s\n' \
        "$HOST" "tar -x --strip-components=1 -C $NEW"
else
    git archive --format=tar HEAD pickup | remote "tar -x --strip-components=1 -C $NEW"
fi
mutate "echo $SHA > $NEW/VERSION"

say "running tests in $NEW"
if [ "$DRY_RUN" -eq 1 ]; then
    mutate "cd $NEW && python3 tests/test_pickup.py 2>&1 | tail -1"
else
    RESULT="$(remote "cd $NEW && python3 tests/test_pickup.py 2>&1 | tail -1")"
    say "tests: $RESULT"
    case "$RESULT" in
        *" passed, 0 failed"*) ;;
        *) die "tests did not pass on the host (left in $NEW for inspection); nothing deployed" ;;
    esac
fi

say "building $IMAGE"
mutate "docker build -q -t $IMAGE --label org.opencontainers.image.revision=$SHA $NEW"

# --- 4. wait for idle and hold the gate ------------------------------------
if [ "$WAIT" -eq 0 ]; then
    warn "--no-wait: recreating the container now; an in-flight handoff will restart from scratch"
elif [ "$DRY_RUN" -eq 0 ] && [ "$(remote "docker inspect -f '{{.State.Running}}' $CONTAINER 2>/dev/null || true")" != "true" ]; then
    say "$CONTAINER is not running; no need to wait"
else
    say "clearing any stale deploy gate, then waiting for the pickup to go idle"
    mutate "docker exec $CONTAINER python3 -c '$RELEASE_PY'"
    mutate "docker exec -d $CONTAINER python3 -c '$GATE_PY'"
    GATE_HELD=1
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'deploy: [dry-run] every %ss until it succeeds: ssh root@%s %s\n' \
            "$POLL_SECONDS" "$HOST" "docker exec $CONTAINER test -f $GATE_FILE"
    else
        until remote "docker exec $CONTAINER test -f $GATE_FILE"; do
            say "pickup busy: $(remote "tail -1 $HOST_LOG 2>/dev/null" || true)"
            sleep "$POLL_SECONDS"
        done
        say "pickup idle; gate held (new ticks skip for up to ${GATE_SECONDS}s)"
    fi
fi

# --- 5. swap and recreate ---------------------------------------------------
say "swapping $NEW into $BUILD_DIR (previous kept as ${BUILD_DIR}.prev) and running run.sh"
SWAPPED=1
mutate "set -e; rm -rf ${BUILD_DIR}.prev; if [ -d $BUILD_DIR ]; then mv $BUILD_DIR ${BUILD_DIR}.prev; fi; mv $NEW $BUILD_DIR; cd $BUILD_DIR && sh ./run.sh"
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

say "done: $(git log -1 --format='%h %s' HEAD) is live"
[ "$DRY_RUN" -eq 1 ] && say "(dry run - nothing was changed on the host)"
exit 0
