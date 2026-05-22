#!/usr/bin/env bash
# SovLens local pre-push validation harness.
#
# Runs the same fast checks that CI runs, before you spend 30 minutes
# watching a release tag fail on a typo. Each step prints clearly what
# passed / failed / skipped. Exits non-zero on any failure.
#
# Usage:
#   ./scripts/validate.sh
#   SKIP_CARGO_CHECK=1 ./scripts/validate.sh   # skip slow Rust check
#
# Optional tools detected at runtime:
#   makensis    (brew install makensis)        — NSIS hook syntax
#   actionlint  (brew install actionlint)      — GitHub Actions YAML

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate repo root regardless of cwd
# ---------------------------------------------------------------------------
if command -v git >/dev/null 2>&1 && git rev-parse --show-toplevel >/dev/null 2>&1; then
  ROOT="$(git rev-parse --show-toplevel)"
else
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT"

# ---------------------------------------------------------------------------
# Color output (only if terminal supports >=8 colors)
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_GREEN="$(tput setaf 2)"
  C_RED="$(tput setaf 1)"
  C_YELLOW="$(tput setaf 3)"
  C_BLUE="$(tput setaf 4)"
  C_BOLD="$(tput bold)"
  C_RESET="$(tput sgr0)"
else
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

PASS=()
FAIL=()
SKIP=()

step() { printf "\n${C_BOLD}${C_BLUE}==> %s${C_RESET}\n" "$1"; }
ok()   { printf "${C_GREEN}  PASS${C_RESET}  %s\n" "$1"; PASS+=("$1"); }
bad()  { printf "${C_RED}  FAIL${C_RESET}  %s\n" "$1"; FAIL+=("$1"); }
skip() { printf "${C_YELLOW}  SKIP${C_RESET}  %s\n" "$1"; SKIP+=("$1"); }

# ---------------------------------------------------------------------------
# (a) NSIS hook lint
# ---------------------------------------------------------------------------
step "NSIS hook lint"
if command -v makensis >/dev/null 2>&1; then
  if ! makensis -CMDHELP >/dev/null 2>&1; then
    bad "makensis -CMDHELP failed (binary present but broken)"
  else
    NSIS_OK=1
    for nsh in frontend/src-tauri/windows/*.nsh; do
      [ -f "$nsh" ] || continue
      TMPDIR_NSIS="$(mktemp -d)"
      STUB="$TMPDIR_NSIS/installer-test.nsi"
      ABS_NSH="$(cd "$(dirname "$nsh")" && pwd)/$(basename "$nsh")"
      cat > "$STUB" <<EOF
OutFile "$TMPDIR_NSIS/dummy.exe"
Name "ValidateStub"
!include "$ABS_NSH"
Section "Stub"
SectionEnd
EOF
      # makensis exits non-zero on syntax error. Build to tmp; suppress output.
      if makensis -NOCD -V2 "$STUB" >"$TMPDIR_NSIS/out.log" 2>&1; then
        ok "NSIS syntax OK: $nsh"
      else
        # The hook file uses macros that NSIS_HOOK_* expect to be called
        # by the Tauri installer template — a bare include may legitimately
        # produce warnings but should not produce hard syntax errors.
        # Treat exit non-zero as failure and surface the log tail.
        bad "NSIS syntax error in $nsh"
        tail -20 "$TMPDIR_NSIS/out.log" | sed 's/^/      /'
        NSIS_OK=0
      fi
      rm -rf "$TMPDIR_NSIS"
    done
    [ "$NSIS_OK" -eq 1 ] || true
  fi
else
  skip "makensis not installed (install: brew install makensis)"
fi

# ---------------------------------------------------------------------------
# (b) GitHub Actions workflow lint
# ---------------------------------------------------------------------------
step "GitHub Actions workflow lint"
WF=".github/workflows/release.yml"
if [ ! -f "$WF" ]; then
  bad "$WF missing"
elif command -v actionlint >/dev/null 2>&1; then
  if actionlint "$WF"; then
    ok "actionlint $WF"
  else
    bad "actionlint reported errors in $WF"
  fi
else
  skip "actionlint not installed (install: brew install actionlint) — falling back to YAML parse"
  if python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$WF" 2>/dev/null; then
    ok "YAML parse OK: $WF"
  else
    # PyYAML may not be installed. Fall back to a manual safe-load via tokenize.
    if python3 - "$WF" <<'PY' 2>/dev/null
import sys
try:
    import yaml
    yaml.safe_load(open(sys.argv[1]))
except ModuleNotFoundError:
    # Best-effort: just verify it's text and contains expected keys.
    txt = open(sys.argv[1]).read()
    assert "jobs:" in txt and "on:" in txt, "missing top-level keys"
PY
    then
      ok "YAML smoke-check OK: $WF (pyyaml not installed)"
    else
      bad "YAML parse failed: $WF"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# (c) PyInstaller spec parse
# ---------------------------------------------------------------------------
step "PyInstaller spec parse"
if (
  cd backend && \
  SOVLENS_ALLOW_PARTIAL_COLLECT=1 python3 -c "exec(open('sovlens-backend.spec').read())" \
  >/tmp/sovlens-spec.log 2>&1
); then
  ok "backend/sovlens-backend.spec parses"
else
  # If failure is solely due to missing PyInstaller helpers (no venv),
  # that's a soft skip — we only care about syntax.
  if grep -qi "No module named 'PyInstaller'" /tmp/sovlens-spec.log; then
    skip "PyInstaller not installed in current python (only syntax/import-time check would catch typos here)"
  else
    bad "backend/sovlens-backend.spec failed to exec — see /tmp/sovlens-spec.log"
    tail -20 /tmp/sovlens-spec.log | sed 's/^/      /'
  fi
fi

# ---------------------------------------------------------------------------
# (d) Backend py_compile
# ---------------------------------------------------------------------------
step "Backend py_compile"
if python3 -m py_compile backend/*.py; then
  ok "backend/*.py compile clean"
else
  bad "backend py_compile failed"
fi

# ---------------------------------------------------------------------------
# (e) Frontend lint
# ---------------------------------------------------------------------------
step "Frontend lint (npm run lint)"
if [ ! -d frontend/node_modules ]; then
  skip "frontend/node_modules missing — run 'cd frontend && npm ci' first"
else
  if ( cd frontend && npm run -s lint ); then
    ok "frontend lint clean"
  else
    bad "frontend lint reported errors"
  fi
fi

# ---------------------------------------------------------------------------
# (f) Frontend TypeScript check
# ---------------------------------------------------------------------------
step "Frontend TypeScript (tsc --noEmit)"
if [ ! -d frontend/node_modules ]; then
  skip "frontend/node_modules missing — run 'cd frontend && npm ci' first"
else
  if ( cd frontend && npx --no-install tsc --noEmit -p tsconfig.json ); then
    ok "tsc --noEmit clean"
  else
    bad "tsc --noEmit reported type errors"
  fi
fi

# ---------------------------------------------------------------------------
# (g) Cargo check
# ---------------------------------------------------------------------------
step "Cargo check (Rust)"
if [ "${SKIP_CARGO_CHECK:-0}" = "1" ]; then
  skip "SKIP_CARGO_CHECK=1 set"
elif ! command -v cargo >/dev/null 2>&1; then
  skip "cargo not installed"
else
  T0=$(date +%s)
  if ( cd frontend/src-tauri && cargo check --release ); then
    T1=$(date +%s)
    ok "cargo check --release ($((T1-T0))s)"
  else
    T1=$(date +%s)
    bad "cargo check --release failed ($((T1-T0))s)"
  fi
fi

# ---------------------------------------------------------------------------
# (h) JSON validate
# ---------------------------------------------------------------------------
step "JSON validate"
JSON_FILES=()
for p in frontend/src-tauri/tauri.conf.json frontend/package.json; do
  [ -f "$p" ] && JSON_FILES+=("$p")
done
if [ -d frontend/src-tauri/capabilities ]; then
  while IFS= read -r f; do JSON_FILES+=("$f"); done < <(find frontend/src-tauri/capabilities -name '*.json')
fi
[ -f package.json ] && JSON_FILES+=("package.json")

if [ "${#JSON_FILES[@]}" -eq 0 ]; then
  skip "no JSON files found"
else
  if python3 -c "import json,sys
for p in sys.argv[1:]:
    json.load(open(p))
    print('  ok', p)
" "${JSON_FILES[@]}"; then
    ok "all ${#JSON_FILES[@]} JSON files valid"
  else
    bad "JSON validation failed"
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf "\n${C_BOLD}==> Summary${C_RESET}\n"
printf "  ${C_GREEN}passed: %d${C_RESET}\n" "${#PASS[@]}"
printf "  ${C_YELLOW}skipped: %d${C_RESET}\n" "${#SKIP[@]}"
printf "  ${C_RED}failed: %d${C_RESET}\n" "${#FAIL[@]}"

if [ "${#FAIL[@]}" -gt 0 ]; then
  printf "\n${C_RED}${C_BOLD}FAILED — fix the above before pushing.${C_RESET}\n"
  for f in "${FAIL[@]}"; do printf "  - %s\n" "$f"; done
  exit 1
fi

printf "\n${C_GREEN}${C_BOLD}ALL CHECKS PASSED — safe to push${C_RESET}\n"
exit 0
