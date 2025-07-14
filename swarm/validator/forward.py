# ---------------------------------------------------------------
# Forward loop for the Swarm validator neuron.
# ---------------------------------------------------------------
from __future__ import annotations

import asyncio
import time
from typing import Dict, List
import traceback
import math          # NEW
import numpy as np
import bittensor as bt

from swarm.protocol import (
    MapTask, FlightPlan, ValidationResult, FlightPlanSynapse,
)
from swarm.utils.uids import get_random_uids

from .task_gen import random_task
from .replay   import replay_once
from .reward   import flight_reward

from swarm.constants import (SIM_DT,      # 50 Hz physics step sent to miners
    HORIZON_SEC,      # max simulated flight time
    SAMPLE_K,         # miners sampled per forward
    QUERY_TIMEOUT,    # dendrite timeout (s)
    FORWARD_SLEEP_SEC,# wait between iterations     
    SAVE_FLIGHTPLANS) # save flight plans to disk

# NEW IMPORT  ───────────────────────────────────────────────────
from .utils import save_flightplans
# ───────────────────────────────────────────────────────────────


# ────────── Internal helpers (use self from outer scope) ────────
BATCH_SIZE = 40  # **maximum miners queried in a single dendrite call**

async def _query_miners(self, task: MapTask) -> dict[int, FlightPlan]:
    """
    Broadcast the MapTask to a random sample of miners and collect the
    returned FlightPlans **in batches of `BATCH_SIZE`** to avoid dendrite
    time‑outs when the sample is large (e.g. 256).

    The public surface of this helper (signature + output) is unchanged.
    """
    # 1. Choose a random sample of miners (uids → axons)
    uids: list[int] = get_random_uids(self, k=SAMPLE_K)
    total_miners    = len(uids)
    num_batches     = math.ceil(total_miners / BATCH_SIZE)

    print(f"Total miners sampled: {total_miners} ➜ "
          f"processing in {num_batches} batch(es) of ≤{BATCH_SIZE}")

    # 2. Prepare container for accumulated FlightPlans
    plans: dict[int, FlightPlan] = {}

    # 3. Loop over batches
    for b in range(num_batches):
        start = b * BATCH_SIZE
        end   = min(start + BATCH_SIZE, total_miners)

        batch_uids   = uids[start:end]
        batch_axons  = [self.metagraph.axons[uid] for uid in batch_uids]

        # Build *fresh* outbound synapse for this batch
        syn = FlightPlanSynapse.from_task(task)
        syn.version = self.version            # propagate protocol version

        print(f"Querying batch #{b + 1} "
              f"({len(batch_axons)} miners: {batch_uids})")

        # 3a. Send query and await replies for this batch
        try:
            replies: list[FlightPlanSynapse] = await self.dendrite(
                axons        = batch_axons,
                synapse      = syn,
                deserialize  = True,
                timeout      = QUERY_TIMEOUT,
            )
        except Exception as e:
            # A failure here means the entire dendrite call timed‑out or crashed.
            # We log the error and continue to the next batch.
            print(f"[ERROR] Dendrite call failed for batch #{b + 1}: {e}")
            traceback.print_exc()
            continue

        # 3b. Extract FlightPlans, skipping invalid / empty replies
        for uid, rep in zip(batch_uids, replies):
            try:
                plan = rep.plan
                plans[uid] = plan
            except Exception as e:
                print(f"[ERROR] Failed to parse plan from miner {uid}: "
                      f"{type(e).__name__} — {e}")
                traceback.print_exc()

    # 4. All batches processed – return the combined dictionary
    return plans


def _score_plan(task: MapTask, uid: int, plan: FlightPlan | None) -> ValidationResult:
    """
    Re‑simulate miner’s trajectory and compute reward components.
    If a miner returned an empty / invalid plan we assign score == 0.
    """
    # ── Treat “no plan” or empty‑command list as an automatic failure ──
    if plan is None or not plan.commands:
        return ValidationResult(
            uid      = uid,
            success  = False,
            time_sec = task.horizon,   # full‑horizon time‑penalty
            energy   = 0.0,
            score    = 0.0,
        )

    # ── Normal scoring path ────────────────────────────────────────────
    success, t_sim, energy = replay_once(task, plan)
    score = flight_reward(success, t_sim, energy, task.horizon)
    return ValidationResult(uid, success, t_sim, energy, score)


def _apply_weight_update(self, results: List[ValidationResult]) -> None:
    """
    Push miners’ scores on‑chain using bittensor’s modern helper methods.

    Assumes your validator class implements:
      • self.update_scores(rewards: np.ndarray, uids: np.ndarray)
      • self.set_weights()              # no arguments
    """
    if not results:
        bt.logging.warning("No validation results – skipping weight update.")
        return

    # Align UIDs and scores
    uids_np    = np.array([r.uid   for r in results], dtype=np.int64)
    scores_np  = np.array([r.score for r in results], dtype=np.float32)

    # Update the scores cache and push weights on‑chain
    self.update_scores(scores_np, uids_np)
    bt.logging.info(f"Updated scores for {len(uids_np)} miners.")


    # ─────── Silent wandb weight logging ───────
    if hasattr(self, 'wandb_helper') and self.wandb_helper:
        try:
            self.wandb_helper.log_weight_update(
                uids=uids_np.tolist(),
                scores=scores_np.tolist()
            )
            bt.logging.debug(f"Weight update logged to wandb for {len(uids_np)} miners")
        except Exception as e:
            bt.logging.debug(f"Wandb weight logging failed: {e}")


# ────────── Public API: called from neurons/validator.py ────────
async def forward(self) -> None:
    """
    One full validator iteration:
      1. build deterministic MapTask
      2. broadcast ➜ collect FlightPlans
      3. replay & score
      4. optionally persist FlightPlans
      5. update on‑chain weights (EMA)
      6. brief sleep
    """
    try:
        # -------- bookkeeping -------------------------------
        if not hasattr(self, "forward_count"):
            self.forward_count = 0
        self.forward_count += 1

        bt.logging.info(f"[Forward #{self.forward_count}] start")

        # -------- 1) build task ------------------------------
        task: MapTask = random_task(sim_dt=SIM_DT, horizon=HORIZON_SEC)
        print("Querying miners")
        # -------- 2) query miners ----------------------------
        plans: Dict[int, FlightPlan] = await _query_miners(self, task)

        # -------- 3) replay & score --------------------------
        print(f"Received {len(plans)} FlightPlans from miners.")
        results: List[ValidationResult] = [
            _score_plan(task, uid, plan) for uid, plan in plans.items()
        ]

        # quick telemetry
        if results:
            best = max(r.score for r in results)
            avg  = sum(r.score for r in results) / len(results)
            bt.logging.info(
                f"Scored {len(results)} miners | best={best:.3f} avg={avg:.3f}"
            )
        else:
            bt.logging.warning("No valid FlightPlans returned by miners.")

        # -------- 4) (optional) persist FlightPlans ----------
        save_flightplans(task, results, plans)

        # -------- 5) silent wandb logging --------------------
        if hasattr(self, 'wandb_helper') and self.wandb_helper:
            try:
                import time
                self.wandb_helper.log_forward_results(
                    forward_count=self.forward_count,
                    task=task,
                    results=results,
                    timestamp=time.time()
                )
                bt.logging.debug(f"Forward #{self.forward_count} logged to wandb ({len(results)} miners)")
            except Exception as e:
                bt.logging.debug(f"Wandb logging failed for forward #{self.forward_count}: {e}")

        # -------- 6) weight update ---------------------------
        _apply_weight_update(self, results)

    except Exception as err:
        bt.logging.error(f"Validator forward error: {err}")

        
        # ─────── Silent wandb error logging ───────
        if hasattr(self, 'wandb_helper') and self.wandb_helper:
            try:
                self.wandb_helper.log_error(
                    f"Validator forward error: {err}",
                    error_type="forward_general"
                )
                bt.logging.debug("Forward error logged to wandb")
            except Exception as log_err:
                bt.logging.debug(f"Failed to log error to wandb: {log_err}")

    # -------- 6) sleep --------------------------------------
    await asyncio.sleep(FORWARD_SLEEP_SEC)
