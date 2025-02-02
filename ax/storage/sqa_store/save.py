#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, Dict, List, Optional, Tuple

from ax.core.base_trial import BaseTrial
from ax.core.experiment import Experiment
from ax.core.generator_run import GeneratorRun
from ax.core.runner import Runner
from ax.core.trial import Trial
from ax.modelbridge.generation_strategy import GenerationStrategy
from ax.storage.sqa_store.db import SQABase, session_scope
from ax.storage.sqa_store.decoder import Decoder
from ax.storage.sqa_store.encoder import Encoder
from ax.storage.sqa_store.sqa_config import SQAConfig
from ax.storage.sqa_store.utils import copy_db_ids
from ax.utils.common.base import Base
from ax.utils.common.logger import get_logger
from ax.utils.common.typeutils import checked_cast, not_none


logger = get_logger(__name__)


def _set_db_ids(obj_to_sqa: List[Tuple[Base, SQABase]]) -> None:
    for obj, sqa_obj in obj_to_sqa:
        if sqa_obj.id is not None:  # pyre-ignore[16]
            obj.db_id = not_none(sqa_obj.id)
        elif obj.db_id is None:
            is_sq_gr = (
                isinstance(obj, GeneratorRun)
                and obj._generator_run_type == "STATUS_QUO"
            )
            # TODO: Remove this warning when storage & perf project is complete.
            if not is_sq_gr:
                logger.warning(
                    f"User-facing object {obj} does not already have a db_id, "
                    f"and the corresponding SQA object: {sqa_obj} does not either."
                )


def save_experiment(experiment: Experiment, config: Optional[SQAConfig] = None) -> None:
    """Save experiment (using default SQAConfig)."""
    if not isinstance(experiment, Experiment):
        raise ValueError("Can only save instances of Experiment")
    if not experiment.has_name:
        raise ValueError("Experiment name must be set prior to saving.")

    config = config or SQAConfig()
    encoder = Encoder(config=config)
    _save_experiment(experiment=experiment, encoder=encoder)


def _save_experiment(
    experiment: Experiment,
    encoder: Encoder,
    return_sqa: bool = False,
    validation_kwargs: Optional[Dict[str, Any]] = None,
) -> Optional[SQABase]:
    """Save experiment, using given Encoder instance.

    1) Convert Ax object to SQLAlchemy object.
    2) Determine if there is an existing experiment with that name in the DB.
    3) If not, create a new one.
    4) If so, update the old one.
        The update works by merging the new SQLAlchemy object into the
        existing SQLAlchemy object, and then letting SQLAlchemy handle the
        actual DB updates.
    """
    exp_sqa_class = encoder.config.class_to_sqa_class[Experiment]
    with session_scope() as session:
        existing_sqa_experiment_id = (
            # pyre-ignore Undefined attribute [16]: `SQABase` has no attribute `id`
            session.query(exp_sqa_class.id)
            .filter_by(name=experiment.name)
            .one_or_none()
        )
    if existing_sqa_experiment_id:
        existing_sqa_experiment_id = existing_sqa_experiment_id[0]

    encoder.validate_experiment_metadata(
        experiment,
        existing_sqa_experiment_id=existing_sqa_experiment_id,
        **(validation_kwargs or {}),
    )

    sqa_experiment, obj_to_sqa = encoder.experiment_to_sqa(experiment)
    with session_scope() as session:
        sqa_experiment = session.merge(sqa_experiment)
        session.flush()

    decoder = Decoder(config=encoder.config)
    new_experiment = decoder.experiment_from_sqa(sqa_experiment)
    copy_db_ids(new_experiment, experiment, [])

    return checked_cast(SQABase, sqa_experiment) if return_sqa else None


def save_generation_strategy(
    generation_strategy: GenerationStrategy, config: Optional[SQAConfig] = None
) -> int:
    """Save generation strategy (using default SQAConfig if no config is
    specified). If the generation strategy has an experiment set, the experiment
    will be saved first.

    Returns:
        The ID of the saved generation strategy.
    """
    # Start up SQA encoder.
    config = config or SQAConfig()
    encoder = Encoder(config=config)

    return _save_generation_strategy(
        generation_strategy=generation_strategy, encoder=encoder
    )


def _save_generation_strategy(
    generation_strategy: GenerationStrategy, encoder: Encoder
) -> int:
    # If the generation strategy has not yet generated anything, there will be no
    # experiment set on it.
    experiment = generation_strategy._experiment
    if experiment is None:
        experiment_id = None
    else:
        # Experiment was set on the generation strategy, so need to check whether
        # if has been saved and create a relationship b/w GS and experiment if so.
        experiment_id = experiment.db_id
        if experiment_id is None:
            raise ValueError(  # pragma: no cover
                f"Experiment {experiment.name} should be saved before "
                "generation strategy."
            )

    gs_sqa, obj_to_sqa = encoder.generation_strategy_to_sqa(
        generation_strategy=generation_strategy, experiment_id=experiment_id
    )

    if generation_strategy._db_id is not None:
        gs_sqa_class = encoder.config.class_to_sqa_class[GenerationStrategy]
        with session_scope() as session:
            existing_gs_sqa = session.query(gs_sqa_class).get(
                generation_strategy._db_id
            )

        # pyre-fixme[16]: `Optional` has no attribute `update`.
        existing_gs_sqa.update(gs_sqa)
        # our update logic ignores foreign keys, i.e. fields ending in _id,
        # because we want SQLAlchemy to handle those relationships for us
        # however, generation_strategy.experiment_id is an exception, so we
        # need to update that manually
        # pyre-fixme[16]: `Optional` has no attribute `experiment_id`.
        existing_gs_sqa.experiment_id = gs_sqa.experiment_id
        gs_sqa = existing_gs_sqa

    with session_scope() as session:
        session.add(gs_sqa)
        session.flush()  # Ensures generation strategy id is set.

    _set_db_ids(obj_to_sqa=obj_to_sqa)

    return not_none(generation_strategy.db_id)


def save_or_update_trial(
    experiment: Experiment, trial: BaseTrial, config: Optional[SQAConfig] = None
) -> None:
    """Add new trial to the experiment, or update if already exists
    (using default SQAConfig)."""
    config = config or SQAConfig()
    encoder = Encoder(config=config)
    _save_or_update_trial(experiment=experiment, trial=trial, encoder=encoder)


def _save_or_update_trial(
    experiment: Experiment, trial: BaseTrial, encoder: Encoder
) -> None:
    """Add new trial to the experiment, or update if already exists."""
    _save_or_update_trials(experiment=experiment, trials=[trial], encoder=encoder)


def save_or_update_trials(
    experiment: Experiment, trials: List[BaseTrial], config: Optional[SQAConfig] = None
) -> None:
    """Add new trials to the experiment, or update if already exists
    (using default SQAConfig)."""
    config = config or SQAConfig()
    encoder = Encoder(config=config)
    _save_or_update_trials(experiment=experiment, trials=trials, encoder=encoder)


def _save_or_update_trials(
    experiment: Experiment, trials: List[BaseTrial], encoder: Encoder
) -> None:
    """Add new trials to the experiment, or update if they already exist."""
    experiment_id = experiment._db_id
    if experiment_id is None:
        raise ValueError("Must save experiment first.")

    data_sqa_class = encoder.config.class_to_sqa_class[
        experiment.default_data_constructor
    ]
    trial_sqa_class = encoder.config.class_to_sqa_class[Trial]
    obj_to_sqa = []
    with session_scope() as session:
        # Fetch the ids of all trials already saved to the experiment
        existing_trial_ids = (
            session.query(trial_sqa_class.id)  # pyre-ignore
            .filter_by(experiment_id=experiment_id)
            .all()
        )

    existing_trial_ids = {x[0] for x in existing_trial_ids}

    update_trial_ids = set()
    update_trial_indices = set()
    for trial in trials:
        if trial._db_id not in existing_trial_ids:
            continue
        update_trial_ids.add(trial._db_id)
        update_trial_indices.add(trial.index)

    # We specifically fetch the *whole* trial (and corresponding data)
    # for old trials that we need to update.
    # We could fetch the whole trial for all trials attached to the experiment,
    # and therefore combine this call with the one above, but that might be
    # unnecessarily costly if we're not updating many or any trials.
    with session_scope() as session:
        existing_trials = (
            session.query(trial_sqa_class)
            .filter(trial_sqa_class.id.in_(update_trial_ids))
            .all()
        )

    with session_scope() as session:
        existing_data = (
            session.query(data_sqa_class)
            .filter_by(experiment_id=experiment_id)
            .filter(data_sqa_class.trial_index.in_(update_trial_indices))  # pyre-ignore
            .all()
        )

    trial_id_to_existing_trial = {trial.id: trial for trial in existing_trials}
    data_id_to_existing_data = {data.id: data for data in existing_data}

    sqa_trials, sqa_datas = [], []
    for trial in trials:
        sqa_trial, _obj_to_sqa = encoder.trial_to_sqa(trial)
        obj_to_sqa.extend(_obj_to_sqa)

        existing_trial = trial_id_to_existing_trial.get(trial._db_id)
        if existing_trial is None:
            sqa_trial.experiment_id = experiment_id
            sqa_trials.append(sqa_trial)
        else:
            existing_trial.update(sqa_trial)
            sqa_trials.append(existing_trial)

        datas = experiment.data_by_trial.get(trial.index, {})
        for ts, data in datas.items():
            sqa_data = encoder.data_to_sqa(
                data=data, trial_index=trial.index, timestamp=ts
            )
            obj_to_sqa.append((data, sqa_data))

            existing_data = data_id_to_existing_data.get(data._db_id)
            if existing_data is None:
                sqa_data.experiment_id = experiment_id
                sqa_datas.append(sqa_data)
            else:
                existing_data.update(sqa_data)
                sqa_datas.append(existing_data)

    with session_scope() as session:
        session.add_all(sqa_trials)
        session.add_all(sqa_datas)
        session.flush()

    _set_db_ids(obj_to_sqa=obj_to_sqa)


def update_generation_strategy(
    generation_strategy: GenerationStrategy,
    generator_runs: List[GeneratorRun],
    config: Optional[SQAConfig] = None,
) -> None:
    """Update generation strategy's current step and attach generator runs
    (using default SQAConfig)."""
    config = config or SQAConfig()
    encoder = Encoder(config=config)
    _update_generation_strategy(
        generation_strategy=generation_strategy,
        generator_runs=generator_runs,
        encoder=encoder,
    )


def _update_generation_strategy(
    generation_strategy: GenerationStrategy,
    generator_runs: List[GeneratorRun],
    encoder: Encoder,
) -> None:
    """Update generation strategy's current step and attach generator runs."""
    gs_sqa_class = encoder.config.class_to_sqa_class[GenerationStrategy]

    gs_id = generation_strategy.db_id
    if gs_id is None:
        raise ValueError("GenerationStrategy must be saved before being updated.")

    if any(gr.db_id for gr in generator_runs):
        raise ValueError("Can only save new GeneratorRuns.")

    experiment_id = generation_strategy.experiment.db_id
    if experiment_id is None:
        raise ValueError(  # pragma: no cover
            f"Experiment {generation_strategy.experiment.name} "
            "should be saved before generation strategy."
        )

    obj_to_sqa = []
    with session_scope() as session:
        session.query(gs_sqa_class).filter_by(id=gs_id).update(
            {
                "curr_index": generation_strategy._curr.index,
                "experiment_id": experiment_id,
            }
        )

    generator_runs_sqa = []
    for generator_run in generator_runs:
        gr_sqa, _obj_to_sqa = encoder.generator_run_to_sqa(generator_run=generator_run)
        obj_to_sqa.extend(_obj_to_sqa)
        gr_sqa.generation_strategy_id = gs_id
        generator_runs_sqa.append(gr_sqa)

    with session_scope() as session:
        session.add_all(generator_runs_sqa)

    _set_db_ids(obj_to_sqa=obj_to_sqa)


def update_runner_on_experiment(
    experiment: Experiment, old_runner: Runner, new_runner: Runner, encoder: Encoder
) -> None:
    runner_sqa_class = encoder.config.class_to_sqa_class[Runner]

    exp_id = experiment.db_id
    if exp_id is None:
        raise ValueError("Experiment must be saved before being updated.")

    old_runner_id = old_runner.db_id

    new_runner_sqa = encoder.runner_to_sqa(runner=new_runner)
    new_runner_sqa.experiment_id = exp_id

    with session_scope() as session:
        if old_runner_id is not None:
            old_runner_sqa = (
                session.query(runner_sqa_class)
                .filter_by(id=old_runner.db_id)
                .one_or_none()
            )
            session.delete(old_runner_sqa)
        session.add(new_runner_sqa)

    _set_db_ids(obj_to_sqa=[(new_runner, new_runner_sqa)])  # pyre-ignore[6]
