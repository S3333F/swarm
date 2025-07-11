# swarm/validator/replay.py
"""
swarm.validator.replay
──────────────────────
Deterministic re‑execution of a miner‑supplied FlightPlan.

• Any physical contact between the drone and another object is considered a
  collision ⇒ the episode is flagged as a failure ⇒ flight_reward() returns 0.
"""
from __future__ import annotations

import time
from typing import Tuple, List

import numpy as np
import pybullet as p

from gym_pybullet_drones.utils.enums import ObservationType, ActionType

from swarm.utils.env_factory import make_env
from swarm.utils.gui_isolation import run_isolated
from swarm.core.drone import track_drone
from swarm.protocol import MapTask, FlightPlan, RPMCmd

# ───────── constants ─────────
from swarm.constants import (
    CAM_HZ,          # camera follow rate
    PROP_EFF,        # propeller efficiency
    WAYPOINT_TOL,    # way-point success tolerance
    LANDING_PLATFORM_RADIUS as _PR,  # platform radius constant
    STABLE_LANDING_SEC,  # required stable landing duration
)
# ─────────────────────────────


# ───────────────── public façade ─────────────────
def replay_once(
    task: MapTask,
    plan: FlightPlan,
    *,
    gui: bool = False,
) -> Tuple[bool, float, float]:
    """Run in an isolated subprocess when required."""
    return run_isolated(_replay_once_impl, task, plan, gui=gui)


# ───────────────── implementation ─────────────────
def _replay_once_impl(
    task: MapTask,
    plan: FlightPlan,
    *,
    gui: bool = False,
) -> Tuple[bool, float, float]:

    # 1 ─ environment ---------------------------------------------------
    env = make_env(task, gui=gui, raw_rpm=True)   # RPM‑controlled
    cli = env.getPyBulletClient()                 # physicsClientId (int)

    # 2 ─ turn the FlightPlan into a step‑indexed RPM table -------------
    last_t = plan.commands[-1].t
    max_steps = int(round(last_t / task.sim_dt)) + 1
    rpm_table = _plan_to_table(plan.commands, max_steps, task.sim_dt)

    # 3 ─ main replay loop ---------------------------------------------
    frames_per_cam = max(1, int(round(1.0 / (task.sim_dt * CAM_HZ))))
    energy = 0.0
    success = False
    collided = False
    stable_landing_time = 0.0  # accumulated stable landing time
    goal = np.asarray(task.goal, dtype=float)
    drone_id = env.DRONE_IDS[0]

    for k in range(max_steps):
        t_sim = k * task.sim_dt
        rpm_vec = rpm_table[k]
        obs, *_ = env.step(rpm_vec[None, :])          # shape (1,4)
        pos = obs[0, :3]

        # camera follow
        if gui and k % frames_per_cam == 0:
            track_drone(cli, drone_id)

        # energy bookkeeping
        energy += (np.square(rpm_vec).sum() * env.KF / PROP_EFF) * task.sim_dt

        # collision check – ignore platform contacts near goal
        if not collided:
            contacts = p.getContactPoints(bodyA=drone_id, physicsClientId=cli)
            if contacts:
                allowed = True
                for cp in contacts:
                    # contact position on A (drone) in world coordinates is cp[5]
                    cpos = cp[5]
                    # Safeguard: ensure tuple length
                    if isinstance(cpos, (list, tuple)) and len(cpos) >= 3:
                        cx, cy, cz = cpos[:3]
                        horiz = np.linalg.norm([cx - goal[0], cy - goal[1]])
                        vert  = abs(cz - goal[2])
                        # If contact is within platform radius and close to surface → allowed
                        if horiz < _PR + 0.05 and vert < 0.3:
                            continue  # allowed contact
                    allowed = False
                    break
                if not allowed:
                    collided = True
                    break  # stop episode early

        # ─ landing success logic: stable landing on green circle/TAO logo ─
        horizontal_distance = np.linalg.norm(pos[:2] - goal[:2])  # X,Y distance only
        vertical_distance = abs(pos[2] - goal[2])                  # Z distance only
        
        # TAO logo now covers 106% of green circle area (from env_builder.py)
        tao_logo_radius = _PR * 0.8 * 1.06  # Green circle radius * TAO coverage
        
        # Check if drone is positioned correctly on large TAO logo surface
        on_tao_logo = (horizontal_distance < tao_logo_radius and   # Within TAO logo
                      vertical_distance < 0.3 and                 # Within 30cm of surface
                      pos[2] >= goal[2] - 0.1)                    # Above platform (not below)
        
        if on_tao_logo:
            # ─ accumulate stable landing time ─
            stable_landing_time += task.sim_dt
            
            # ─ success condition: stable for required duration ─
            if stable_landing_time >= STABLE_LANDING_SEC:
                success = True
                break
        else:
            # ─ reset landing timer if drone moves away ─
            stable_landing_time = 0.0

        if gui:
            time.sleep(task.sim_dt)

    if not gui:
        env.close()

    # Any collision ⇒ failure (success = False)
    if collided:
        success = False

    return success, t_sim, energy


# ───────────────── helpers ───────────────────────
def _plan_to_table(
    cmds: List[RPMCmd],
    max_steps: int,
    sim_dt: float,
) -> np.ndarray:
    """
    Convert the ragged list of (t, rpm) commands into a fully populated
    (max_steps × 4) numpy array, holding the last known RPM once the plan ends.
    """
    table = np.zeros((max_steps, 4), dtype=float)
    last = np.zeros(4, dtype=float)
    idx = 0

    for cmd in cmds:
        k = int(cmd.t / sim_dt + 1e-9)
        k = max(0, min(k, max_steps - 1))  # clip

        # fill gap up to (but not including) k
        if k > idx:
            table[idx:k, :] = last
        # new rpm at k
        last = np.asarray(cmd.rpm, dtype=float)
        table[k, :] = last
        idx = k + 1

    # pad remaining steps
    if idx < max_steps:
        table[idx:, :] = last

    return table
