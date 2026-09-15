# Runtime model identity

Project: **Newton_Local**. Production routing alias: **local-model**. Human
service label: **AI Inference**. Model names identify catalog entries and the
verified resident, never a permanent endpoint identity.

`scripts/resident_identity.py` and `/api/identity` are read-only. They bind the
registered artifact to the LaunchAgent PID, process start time/arguments, socket
owner and backend metadata. The generation changes when the process or verified
identity changes. Stopped/unknown records have no resident ID. This is operational
residency verification plus manifest binding, not an in-memory weight hash proof.

llama.cpp provides the loaded path, capabilities and context through `/props`.
mlx-serve requires the matching pack's `loaded=true,state=ready` catalog row
and matching `/props` context. The portable adapters use MLX loaded-provider
metadata; a list of downloaded models alone is insufficient.

Hermes routes to `local-model` but receives a separate verified identity block.
The persisted prompt is invalidated by resident generation. A generation change
inside a turn fails the turn rather than attributing its result to another model.
Vision and context follow the verified record. Execution guidance is an explicit
local-profile policy and does not depend on model-name substring matching.

## Reproduction

The upstream Hermes source identity and exact patch hashes are in
`integrations/hermes-patch.json`; source changes are in `integrations/hermes.patch`.
Shared modules live in `integrations/newton_runtime/`. Install into the pinned
Hermes runtime with:

```sh
python3 scripts/install-hermes-integration.py --runtime "$HOME/.hermes/hermes-agent"
python3 scripts/install-hermes-integration.py --runtime "$HOME/.hermes/hermes-agent" --check
```

`install-hermes-localchat.sh` applies the integration, merges the profile,
removes the old static vision override, installs the current operations skill,
and removes the specifically audited active-memory assertions. It does not
rewrite historical transcripts. Reapplying refuses unknown upstream source;
updates require reviewing/rebasing the tracked patch.

## Verification

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/test-model-registry.py
python3 -B scripts/test-qwenctl-logic.py
python3 -B scripts/prove-runtime-identity.py
```

The last command performs live 27B → Flash-Next → 27B proving in one Hermes
session through the control API, then stops the model and restores the starting
selection. Run only on the active Mac Pro, never on portable GLM installations.
Raw proof records remain untracked in `state/runtime-identity-proof.json`.
