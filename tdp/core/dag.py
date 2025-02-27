# Copyright 2022 TOSIT.IO
# SPDX-License-Identifier: Apache-2.0

"""
The `Dag` class reads YAML from collection's dag files
and validates it according to operations rules(cf. operations' rules section)
to build the DAG.

It is used to get a list of operations by performing a topological sort on the DAG
or on a subgraph of the DAG.
"""

import fnmatch
import functools
import logging
import re
from collections import OrderedDict
from pathlib import Path

import networkx as nx
import yaml

from tdp.core.collection import Collection
from tdp.core.operation import Operation

try:
    from yaml import CDumper as Dumper
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Dumper, Loader


logger = logging.getLogger("tdp").getChild("dag")

SERVICE_PRIORITY = {
    "exporter": 1,
    "zookeeper": 2,
    "hadoop": 3,
    "ranger": 4,
    "hdfs": 5,
    "yarn": 6,
    "hive": 7,
    "hbase": 8,
    "spark": 9,
    "spark3": 10,
    "knox": 11,
}
DEFAULT_SERVICE_PRIORITY = 99


class Dag:
    """Generate DAG with operations' dependencies"""

    def __init__(self, collections):
        """
        :param collections: ordered mapping of collections, with names used as keys
        :type collections: OrderedDict[str, Collection]
        """
        self._collections = collections
        self._operations = None
        self._graph = None
        self._yaml_files = None
        self._services = None
        self._services_operations = None

    @staticmethod
    def from_collection(collection):
        """Factory method to build a dag from a single collection. Lenient on input type

        :param collection: one collection
        :type collection: Union[str, Path, Collection]
        :raises ValueError: if invalid type
        :return: Dag built from input
        :rtype: Dag
        """
        if isinstance(collection, (str, Path)):
            return Dag.from_collections([Collection.from_path(collection)])
        elif isinstance(collection, Collection):
            return Dag.from_collections([collection])
        raise ValueError("collection must be either an str, a Path or a Collection")

    @staticmethod
    def from_collections(collections):
        """Factory method to build a dag from multiple collections

        Ordering of the sequence is what will determine the loading order of the operations.

        :param collections: Ordered Sequence of Collection
        :type collections: Sequence[Collection]
        :return: Dag built from x collections
        :rtype: Dag
        """
        collections = OrderedDict(
            (collection.name, collection) for collection in collections
        )

        return Dag(collections)

    @property
    def collections(self):
        return self._collections

    @collections.setter
    def collections(self, collections):
        self._collections = collections
        del self.operations

    @property
    def operations(self):
        if self._operations is not None:
            return self._operations

        operations = {}
        for collection_name, collection in self._collections.items():
            operations_list = []
            for yaml_file in collection.dag_yamls:
                with yaml_file.open("r") as operation_file:
                    operations_list.extend(
                        yaml.load(operation_file, Loader=Loader) or []
                    )

            for operation in operations_list:
                name = operation["name"]
                if name in operations:
                    raise ValueError(
                        (
                            f'"{name}" is declared at least twice,'
                            f" first in {operations[name].collection_name}, "
                            f" second in {collection_name}"
                        )
                    )
                operations[name] = Operation(
                    collection_name=collection_name, **operation
                )

        self._operations = operations
        self.validate()
        return self._operations

    @operations.setter
    def operations(self, value):
        self._operations = value
        del self.graph
        del self.services_operations
        del self.services

    @operations.deleter
    def operations(self):
        self.operations = None

    @property
    def services_operations(self):
        if self._services_operations is None:
            self._services_operations = {}
            for operation in self.operations.values():
                self._services_operations.setdefault(operation.service, []).append(
                    operation
                )
        return self._services_operations

    @services_operations.deleter
    def services_operations(self):
        self._services_operations = None
        del self.services

    @property
    def services(self):
        if self._services is None:
            self._services = list(self.services_operations.keys())
        return self._services

    @services.deleter
    def services(self):
        self._services = None

    @property
    def graph(self):
        if self._graph is not None:
            return self._graph

        operation_names = sorted(self.operations.keys())
        DG = nx.DiGraph()
        DG.add_nodes_from(operation_names)

        for operation_name in operation_names:
            operation = self.operations[operation_name]
            for dependency in sorted(operation.depends_on):
                if dependency not in self.operations:
                    raise ValueError(
                        f'Dependency "{dependency}" does not exist for operation "{operation_name}"'
                    )
                DG.add_edge(dependency, operation_name)

        if nx.is_directed_acyclic_graph(DG):
            self._graph = DG
            return self._graph
        else:
            raise ValueError("Not a DAG")

    @graph.setter
    def graph(self, value):
        self._graph = value

    @graph.deleter
    def graph(self):
        self.graph = None

    def topological_sort(self, nodes=None):
        graph = self.graph
        if nodes:
            graph = self.graph.subgraph(nodes)

        def custom_key(node):
            operation = self.operations[node]
            operation_priority = SERVICE_PRIORITY.get(
                operation.service, DEFAULT_SERVICE_PRIORITY
            )
            return f"{operation_priority:02d}_{node}"

        return list(nx.lexicographical_topological_sort(graph, custom_key))

    def get_operations(self, sources=None, targets=None):
        if sources:
            return self.get_operations_from_nodes(sources)
        elif targets:
            return self.get_operations_to_nodes(targets)
        return self.get_all_operations()

    def get_operations_to_nodes(self, nodes):
        nodes_set = set(nodes)
        for node in nodes:
            nodes_set.update(nx.ancestors(self.graph, node))
        return self.topological_sort(nodes_set)

    def get_operations_from_nodes(self, nodes):
        nodes_set = set(nodes)
        for node in nodes:
            nodes_set.update(nx.descendants(self.graph, node))
        return self.topological_sort(nodes_set)

    def get_all_operations(self):
        """gets all operations from the graph sorted topologically and lexicographically.

        :return: a topologically and lexicographically sorted string list
        :rtype: List[str]
        """
        return self.topological_sort(self.graph)

    def filter_operations_glob(self, operations, glob):
        return fnmatch.filter(operations, glob)

    def filter_operations_regex(self, operations, regex):
        compiled_regex = re.compile(regex)
        return list(filter(compiled_regex.match, operations))

    def validate(self):
        r"""Validation rules :
        - \*_start operations can only be required from within its own service
        - \*_install operations should only depend on other \*_install operations
        - Each service (HDFS, HBase, Hive, etc) should have \*_install, \*_config, \*_init and \*_start operations even if they are "empty" (tagged with noop)
        - Operations tagged with the noop flag should not have a playbook defined in the collection
        - Each service action (config, start, init) except the first (install) must have an explicit dependency with the previous service operation within the same service
        """
        # key: service_name
        # value: set of available actions for the service
        services_actions = {}

        def warning(collection_name, message):
            logger.warning(message + f", collection: {collection_name}")

        for operation_name, operation in self.operations.items():
            c_warning = functools.partial(warning, operation.collection_name)
            for dependency in operation.depends_on:
                # *_start operations can only be required from within its own service
                dependency_service = self.operations[dependency].service
                if (
                    dependency.endswith("_start")
                    and dependency_service != operation.service
                ):
                    c_warning(
                        f"Operation '{operation_name}' is in service '{operation.service}', depends on "
                        f"'{dependency}' which is a start action in service '{dependency_service}' and should "
                        f"only depends on start action within its own service"
                    )

                # *_install operations should only depend on other *_install operations
                if operation_name.endswith("_install") and not dependency.endswith(
                    "_install"
                ):
                    c_warning(
                        f"Operation '{operation_name}' is an install action, depends on '{dependency}' which is "
                        f"not an install action and should only depends on other install action"
                    )

            # Each service (HDFS, HBase, Hive, etc) should have *_install, *_config, *_init and *_start actions
            # even if they are "empty" (tagged with noop)
            # Part 1
            service_actions = services_actions.setdefault(operation.service, set())
            if operation.is_service():
                service_actions.add(operation.action)

                # Each service action (config, start, init) except the first (install) must have an explicit
                # dependency with the previous service action within the same service
                actions_order = ["install", "config", "start", "init"]
                # Check only if the action is in actions_order and is not the first
                if (
                    operation.action in actions_order
                    and operation.action != actions_order[0]
                ):
                    previous_action = actions_order[
                        actions_order.index(operation.action) - 1
                    ]
                    previous_service_action = f"{operation.service}_{previous_action}"
                    previous_service_action_found = False
                    # Loop over dependency and check if the service previous action is found
                    for dependency in operation.depends_on:
                        if dependency == previous_service_action:
                            previous_service_action_found = True
                    if not previous_service_action_found:
                        c_warning(
                            f"Operation '{operation_name}' is a service action and has to depend on "
                            f"'{operation.service}_{previous_action}'"
                        )

            # Operations tagged with the noop flag should not have a playbook defined in the collection

            if (
                operation_name
                in self._collections[operation.collection_name].operations
            ):
                if operation.noop:
                    c_warning(
                        f"Operation '{operation_name}' is noop and the playbook should not exist"
                    )
            else:
                if not operation.noop:
                    c_warning(f"Operation '{operation_name}' should have a playbook")

        # Each service (HDFS, HBase, Hive, etc) should have *_install, *_config, *_init and *_start actions
        # even if they are "empty" (tagged with noop)
        # Part 2
        actions_for_service = {"install", "config", "start", "init"}
        for service, actions in services_actions.items():
            if not actions.issuperset(actions_for_service):
                logger.warning(
                    f"Service '{service}' have these actions {actions} and at least one action is missing from "
                    f"{actions_for_service}"
                )
