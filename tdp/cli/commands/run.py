# Copyright 2022 TOSIT.IO
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

import click

from tdp.cli.session import get_session_class
from tdp.cli.utils import check_services_cleanliness, collection_paths
from tdp.core.dag import Dag
from tdp.core.runner.ansible_executor import AnsibleExecutor
from tdp.core.runner.operation_runner import OperationRunner
from tdp.core.service_manager import ServiceManager


@click.command(short_help="Run single TDP operation")
@click.argument("node")
@click.option(
    "--sqlite-path",
    envvar="TDP_SQLITE_PATH",
    type=Path,
    help="Path to SQLITE database file",
    required=True,
)
@click.option(
    "--collection-path",
    envvar="TDP_COLLECTION_PATH",
    required=True,
    callback=collection_paths,  # transforms list of path into list of Collection
    help=f"List of paths separated by your os' path separator ({os.pathsep})",
)
@click.option(
    "--run-directory",
    envvar="TDP_RUN_DIRECTORY",
    type=Path,
    help="Working directory where the executor is launched (`ansible-playbook` for Ansible)",
    required=True,
)
@click.option(
    "--vars", envvar="TDP_VARS", type=Path, help="Path to the tdp vars", required=True
)
@click.option("--dry", is_flag=True, help="Execute dag without running any operation")
def run(
    node,
    sqlite_path,
    collection_path,
    run_directory,
    vars,
    dry,
):
    if not vars.exists():
        raise click.BadParameter(f"{vars} does not exist")
    dag = Dag.from_collections(collection_path)

    operation = dag.operations.get(node, None)
    if not operation:
        raise click.BadParameter(f"{node} is not a valid node")

    if operation.noop:
        raise click.BadParameter(
            f"{node} is tagged as noop and thus"
            " cannot be executed in an unitary deployment"
        )

    run_directory = run_directory.absolute() if run_directory else None

    ansible_executor = AnsibleExecutor(
        run_directory=run_directory,
        dry=dry,
    )
    session_class = get_session_class(sqlite_path)
    with session_class() as session:
        service_managers = ServiceManager.get_service_managers(dag, vars)
        check_services_cleanliness(service_managers)

        operation_runner = OperationRunner(dag, ansible_executor, service_managers)
        click.echo(f"Deploying {node}")
        deployment = operation_runner.run_nodes(targets=[node], node_filter=node)
        session.add(deployment)
        session.commit()
