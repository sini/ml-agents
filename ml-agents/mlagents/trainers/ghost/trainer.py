# # Unity ML-Agents Toolkit
# ## ML-Agent Learning (Ghost Trainer)

# import logging
from typing import Dict, List, Any, cast

import numpy as np
import logging

from mlagents.trainers.brain import BrainParameters
from mlagents.trainers.policy import Policy
from mlagents.trainers.tf_policy import TFPolicy

from mlagents.trainers.trainer import Trainer
from mlagents.trainers.trajectory import Trajectory
from mlagents.trainers.agent_processor import AgentManagerQueue

from mlagents.tf_utils.tf import TensorFlowVariables

# logger = logging.getLogger("mlagents.trainers")

LOGGER = logging.getLogger("mlagents.trainers")


class GhostTrainer(Trainer):
    def __init__(
        self, trainer, brain_name, reward_buff_cap, trainer_parameters, training, run_id
    ):
        """
        Responsible for collecting experiences and training trainer model via self_play.
        :param trainer: The trainer of the policy/policies being trained with self_play
        :param brain_name: The name of the brain associated with trainer config
        :param reward_buff_cap: Max reward history to track in the reward buffer
        :param trainer_parameters: The parameters for the trainer (dictionary).
        :param training: Whether the trainer is set for training.
        :param run_id: The identifier of the current run
        """

        super(GhostTrainer, self).__init__(
            brain_name, trainer_parameters, training, run_id, reward_buff_cap
        )

        self.trainer = trainer

        self.internal_policy_queues: List[AgentManagerQueue[Policy]] = []
        self.internal_trajectory_queues: List[AgentManagerQueue[Trajectory]] = []

        # assign ghost's stats collection to wrapped trainer's
        self.stats_reporter = self.trainer.stats_reporter

        self_play_parameters = trainer_parameters["ghost"]
        self.window = self_play_parameters["window"]
        self.current_prob = self_play_parameters["current_prob"]
        self.steps_between_snapshots = self_play_parameters["snapshot_per"]

        self.policies: Dict[str, TFPolicy] = {}
        self.policy_snapshots: List[Any] = [None for _ in range(self.window)]
        self.snapshot_counter: int = 0
        self.learning_behavior_name: str = None
        self.current_policy_snapshot = None
        self.last_step = 0

        self.initial_elo: float = 1200.0
        self.current_elo: float = self.initial_elo
        self.policy_elos: List[float] = [self.initial_elo for _ in range(self.window)]
        self.current_opponent: int = 0

    @property
    def get_step(self) -> int:
        """
        Returns the number of steps the trainer has performed
        :return: the step count of the trainer
        """
        return self.trainer.get_step

    def _write_summary(self, step: int) -> None:
        """
        Saves training statistics to Tensorboard.
        """
        opponents = np.array(self.policy_elos)
        LOGGER.info(
            " ELO: {}\n"
            "Mean Opponent ELO: {}"
            " Std Opponent ELO: {}".format(
                self.current_elo, opponents.mean(), opponents.std()
            )
        )
        self.stats_reporter.add_stat("ELO", self.current_elo)

    def _process_trajectory(self, trajectory: Trajectory) -> None:
        if (
            trajectory.done_reached
            and not trajectory.max_step_reached
            and self.current_opponent > -1
        ):
            result = "win"
            final_reward = trajectory.steps[-1].reward
            if final_reward < 0:
                result = "loss"
            change = compute_elo_rating_changes(
                self.current_elo, self.policy_elos[self.current_opponent], result
            )
            self.current_elo += change
            self.policy_elos[self.current_opponent] -= change

    def _is_ready_update(self) -> bool:
        pass

    def _update_policy(self) -> None:
        pass

    def advance(self) -> None:
        """
        Steps the trainer, taking in trajectories and updates if ready.
        """
        for traj_queue, internal_traj_queue in zip(
            self.trajectory_queues, self.internal_trajectory_queues
        ):
            try:
                t = traj_queue.get_nowait()
                # adds to wrapped trainers queue
                internal_traj_queue.put(t)
                self._process_trajectory(t)
            except AgentManagerQueue.Empty:
                pass

        self.next_update_step = self.trainer.next_update_step
        self.trainer.advance()
        self._maybe_write_summary(self.get_step)

        for q, internal_q in zip(self.policy_queues, self.internal_policy_queues):
            # Get policies that correspond to the policy queue in question
            try:
                policy = cast(TFPolicy, internal_q.get_nowait())
                with policy.graph.as_default():
                    weights = policy.tfvars.get_weights()
                    self.current_policy_snapshot = weights
                q.put(policy)
            except AgentManagerQueue.Empty:
                pass

        if self.get_step - self.last_step > self.steps_between_snapshots:
            self.save_snapshot(self.trainer.policy)
            self.last_step = self.get_step
            self.swap_snapshots()

    def end_episode(self):
        self.trainer.end_episode()

    def save_model(self, name_behavior_id: str) -> None:
        self.trainer.save_model(name_behavior_id)

    def export_model(self, name_behavior_id: str) -> None:
        self.trainer.export_model(name_behavior_id)

    def create_policy(self, brain_parameters: BrainParameters) -> TFPolicy:
        return self.trainer.create_policy(brain_parameters)

    def add_policy(self, name_behavior_id: str, policy: TFPolicy) -> None:
        # for saving/swapping snapshots
        with policy.graph.as_default():
            policy.tfvars = TensorFlowVariables(policy.model.output, policy.sess)

        self.policies[name_behavior_id] = policy

        # First policy encountered
        if not self.learning_behavior_name:
            with policy.graph.as_default():
                weights = policy.tfvars.get_weights()
                self.current_policy_snapshot = weights
            for i in range(self.window):
                self.policy_snapshots[i] = weights
            self.trainer.add_policy(name_behavior_id, policy)
            self.learning_behavior_name = name_behavior_id

    def get_policy(self, name_behavior_id: str) -> TFPolicy:
        return self.policies[name_behavior_id]

    def save_snapshot(self, policy: TFPolicy) -> None:
        with policy.graph.as_default():
            weights = policy.tfvars.get_weights()
            self.policy_snapshots[self.snapshot_counter] = weights
        self.policy_elos[self.snapshot_counter] = self.current_elo
        self.snapshot_counter = (self.snapshot_counter + 1) % self.window

    def swap_snapshots(self) -> None:
        for q in self.policy_queues:
            name_behavior_id = q.behavior_id
            # here is the place for a sampling protocol
            if name_behavior_id == self.learning_behavior_name:
                continue
            elif np.random.uniform() < (1 - self.current_prob):
                x = np.random.randint(len(self.policy_snapshots))
                snapshot = self.policy_snapshots[x]
            else:
                snapshot = self.current_policy_snapshot
                x = "current"
            self.current_opponent = -1 if x == "current" else x
            print(
                "Step {}: Swapping snapshot {} to id {} with {} learning".format(
                    self.get_step, x, name_behavior_id, self.learning_behavior_name
                )
            )
            policy = self.get_policy(name_behavior_id)
            with policy.graph.as_default():
                policy.tfvars.set_weights(snapshot)
            # not necessary in the single machine case
            q.put(policy)

    def publish_policy_queue(self, policy_queue: AgentManagerQueue[Policy]) -> None:
        """
        Adds a policy queue to the list of queues to publish to when this Trainer
        makes a policy update
        :param queue: Policy queue to publish to.
        """
        super().publish_policy_queue(policy_queue)
        if policy_queue.behavior_id == self.learning_behavior_name:

            internal_policy_queue: AgentManagerQueue[Policy] = AgentManagerQueue(
                policy_queue.behavior_id
            )

            self.internal_policy_queues.append(internal_policy_queue)
            self.trainer.publish_policy_queue(internal_policy_queue)

    def subscribe_trajectory_queue(
        self, trajectory_queue: AgentManagerQueue[Trajectory]
    ) -> None:
        if trajectory_queue.behavior_id == self.learning_behavior_name:
            super().subscribe_trajectory_queue(trajectory_queue)

            internal_trajectory_queue: AgentManagerQueue[
                Trajectory
            ] = AgentManagerQueue(trajectory_queue.behavior_id)

            self.internal_trajectory_queues.append(internal_trajectory_queue)
            self.trainer.subscribe_trajectory_queue(internal_trajectory_queue)


# Taken from https://github.com/Unity-Technologies/ml-agents/pull/1975
# ELO calculation
K = 1  # Constant for rating changes, higher is less stable but converges faster


def compute_elo_rating_changes(rating1, rating2, result):
    r1 = pow(10, rating1 / 400)
    r2 = pow(10, rating2 / 400)

    summed = r1 + r2
    e1 = r1 / summed

    s1 = 1 if result == "win" else 0

    change = K * (s1 - e1)
    #    change = -change if result != "win" else change
    return change