#!/usr/bin/env bash
# A git repo on main with a generic crew: the plugin's SessionStart hooks load the L0.
set -eu
git init -q -b main .
git config user.email eval@example.invalid
git config user.name eval
mkdir -p .weside docs/plans
cat > .weside/crew.json <<'J'
{
  "version": 2,
  "human": {"name": "Hal", "decides": ["vision", "money", "prod"]},
  "naming": {"key": "ACME-\\d+"},
  "roles": {"product": "ada", "architecture": "vic", "acceptance": "tess", "machinery": "kit",
            "coordinator": "otto", "escalation": "nora", "hr": "hana"},
  "members": {
    "ada": {"name": "Ada", "workplace": "."}, "vic": {"name": "Vic", "workplace": "."},
    "tess": {"name": "Tess", "workplace": "."}, "kit": {"name": "Kit", "workplace": "."},
    "otto": {"name": "Otto", "workplace": "."}, "nora": {"name": "Nora", "workplace": "."},
    "hana": {"name": "Hana", "workplace": "."}
  }
}
J
printf '# acme\n\nA small demo app.\n' > README.md
git add -A && git commit -qm init
