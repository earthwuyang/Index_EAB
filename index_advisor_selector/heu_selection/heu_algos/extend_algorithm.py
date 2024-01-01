import copy
import logging

from .selection_algorithm import DEFAULT_PARAMETER_VALUES, SelectionAlgorithm

from index_eab.eab_algo.heu_selection.heu_utils.index import Index
from index_eab.eab_algo.heu_selection.heu_utils.selec_com import b_to_mb, mb_to_b

# budget_MB: The algorithm can utilize the specified storage budget in MB.
# max_index_width: The number of columns an index can contain at maximum.
# min_cost_improvement: The value of the relative improvement that must be realized by a
#                       new configuration to be selected.
# The algorithm stops if either the budget is exceeded or no further beneficial
# configurations can be found.
DEFAULT_PARAMETERS = {
    "budget_MB": DEFAULT_PARAMETER_VALUES["budget_MB"],
    "max_index_width": DEFAULT_PARAMETER_VALUES["max_index_width"],
    "min_cost_improvement": 1.003
}


# This algorithm is a reimplementation of the Extend heuristic published by Schlosser,
# Kossmann, and Boissier in 2019.
# Details can be found in the original paper:
# Rainer Schlosser, Jan Kossmann, Martin Boissier: Efficient Scalable
# Multi-attribute Index Selection Using Recursive Strategies. ICDE 2019: 1238-1249
class ExtendAlgorithm(SelectionAlgorithm):
    def __init__(self, database_connector, parameters=None, process=False,
                 cand_gen=None, is_utilized=None, sel_oracle=None):
        if parameters is None:
            parameters = {}
        SelectionAlgorithm.__init__(
            self, database_connector, parameters, DEFAULT_PARAMETERS,
            process, cand_gen, is_utilized, sel_oracle
        )
        self.budget = mb_to_b(self.parameters["budget_MB"])
        self.max_index_width = self.parameters["max_index_width"]
        self.workload = None
        self.min_cost_improvement = self.parameters["min_cost_improvement"]

        # todo(0804): newly added. for number.
        self.max_indexes = self.parameters["max_indexes"]
        self.constraint = self.parameters["constraint"]

    def _calculate_best_indexes(self, workload, db_conf=None, columns=None):
        """
        1. First round: single-column index.
        2. Second+ round: single-column index / multi-column index(based on the expansion of current index).
        :param workload:
        :return:
        """
        logging.info("Calculating best indexes Extend")

        # todo(0804): newly added. for storage budget/number.
        if (self.constraint == "number" and self.max_indexes == 0) or \
                (self.constraint == "storage" and self.budget == 0):
            return list()

        self.workload = workload

        # Single-column index (called in AutoAdmin)
        single_attribute_index_candidates = self.workload.potential_indexes()
        extension_attribute_candidates = single_attribute_index_candidates.copy()

        # todo: newly added. for process visualization.
        if self.process:
            self.step["candidates"] = single_attribute_index_candidates

        # Current index combination
        index_combination = []
        index_combination_size = 0

        # Best index combination during enumeration/evaluation step
        best = {"combination": [], "benefit_to_size_ratio": 0, "cost": None}

        current_cost = self.cost_evaluation.calculate_cost(
            self.workload, index_combination, store_size=True
        )
        self.initial_cost = current_cost

        # Breaking when no cost improvement
        # todo: newly added. for process visualization.
        if self.process:
            self.layer = 0
        while True:
            # todo: newly added. for process visualization.
            if self.process:
                self.step[self.layer] = list()

            # todo(0804): newly added. for number.
            if self.constraint == "number" and len(index_combination) >= self.max_indexes:
                break

            single_attribute_index_candidates = self._get_candidates_within_budget(
                index_combination_size, single_attribute_index_candidates
            )
            # 1. single-column index.
            for candidate in single_attribute_index_candidates:
                # Only single column index generation
                if candidate not in index_combination:
                    ratio = self._evaluate_combination(
                        index_combination + [candidate], best, current_cost
                    )
                    # todo: newly added. for process visualization.
                    if self.process:
                        self.step[self.layer].append({"combination": index_combination + [candidate],
                                                      "candidate": candidate,
                                                      "oracle": ratio})

            # 2. append column to the existed index.
            # first round: index_combination is `[]`.
            for attribute in extension_attribute_candidates:
                # Multi column indexes are generated by
                # attaching columns to existing indexes
                self._attach_to_indexes(index_combination, attribute, best, current_cost)

            # todo(1216): no useful index, no useful single-column index -> exit.
            if best["benefit_to_size_ratio"] <= 0:
                break

            # todo: newly added. for process visualization.
            if self.process:
                self.step["selected"].append([item["combination"] for item in
                                              self.step[self.layer]].index(best["combination"]))
                self.layer += 1

            index_combination = best["combination"]
            index_combination_size = sum(
                index.estimated_size for index in index_combination
            )
            logging.debug(
                "Add index. Current cost savings: "
                f"{(1 - best['cost'] / current_cost) * 100:.3f}, "
                f"initial {(1 - best['cost'] / self.initial_cost) * 100:.3f}. "
                f"Current storage: {index_combination_size:.2f}"  # f"{b_to_mb(index_combination_size):.2f}MB"
            )

            best["benefit_to_size_ratio"] = 0
            current_cost = best["cost"]

        return index_combination

    def _attach_to_indexes(self, index_combination, attribute, best, current_cost):
        assert (
                attribute.is_single_column() is True
        ), "Attach to indexes called with multi column index"

        # first round: index_combination is `[]`.
        for position, index in enumerate(index_combination):
            if len(index.columns) >= self.max_index_width:
                continue
            if index.appendable_by(attribute):
                new_index = Index(index.columns + attribute.columns)
                if new_index in index_combination:
                    continue
                new_combination = copy.deepcopy(index_combination)  # .copy()
                # We don't replace, but del and append to keep track of the append order?
                del new_combination[position]
                new_combination.append(new_index)
                ratio = self._evaluate_combination(
                    new_combination,
                    best,
                    current_cost,
                    index_combination[position].estimated_size,
                )
                # try:
                #     new_combination.append(new_index)
                #     self._evaluate_combination(
                #         new_combination,
                #         best,
                #         current_cost,
                #         index_combination[position].estimated_size,
                #     )
                # except:
                #     print(1)

                # todo: newly added. for process visualization.
                if self.process:
                    self.step[self.layer].append({"combination": new_combination,
                                                  "candidate": new_index,
                                                  "oracle": ratio})

    def _get_candidates_within_budget(self, index_combination_size, candidates):
        if self.constraint == "storage":
            new_candidates = list()
            for candidate in candidates:
                if (candidate.estimated_size is None) or \
                        (candidate.estimated_size + index_combination_size <= self.budget):
                    new_candidates.append(candidate)
            return new_candidates
        elif self.constraint == "number":
            return candidates

    def _evaluate_combination(
            self, index_combination, best, current_cost, old_index_size=0
    ):
        cost = self.cost_evaluation.calculate_cost(
            self.workload, index_combination, store_size=True
        )
        # (current_cost / cost) <= self.min_cost_improvement -> return,
        # at least 0.003 (0.3%) / 0.997 cost improvement.
        if (cost * self.min_cost_improvement) >= current_cost:
            # todo: newly modified. return -> return -1
            # return
            return -1
        benefit = current_cost - cost
        new_index = index_combination[-1]  # the candidate input.

        # todo: newly added. HypoPG estimation error?
        try:
            new_index_size_difference = new_index.estimated_size - old_index_size
        except:
            logging.error("estimated_size is None!")
            ind_key = list(self.cost_evaluation.current_indexes).index(new_index)
            new_index_curr = list(self.cost_evaluation.current_indexes)[ind_key]
            new_index_size_difference = new_index_curr.estimated_size - old_index_size
            index_combination[-1] = new_index_curr

        # might be the same for the small table.
        if new_index_size_difference == 0:
            new_index_size_difference = 1
            logging.error(f"Index `({new_index})` size difference should not be 0!")
        # assert new_index_size_difference != 0, "Index size difference should not be 0!"

        # todo(0917): newly added.
        if self.sel_oracle is None:
            ratio = benefit / b_to_mb(new_index_size_difference)
        elif self.sel_oracle == "cost_per_sto":
            ratio = -1 * cost * b_to_mb(new_index_size_difference)
        elif self.sel_oracle == "cost_pure":
            ratio = -1 * cost
        elif self.sel_oracle == "benefit_per_sto":
            ratio = benefit / b_to_mb(new_index_size_difference)
        elif self.sel_oracle == "benefit_pure":
            ratio = benefit

        total_size = sum(index.estimated_size for index in index_combination)
        if ratio > best["benefit_to_size_ratio"] and total_size <= self.budget:
            logging.debug(f"new best cost and size: {cost}\t" f"{b_to_mb(total_size):.2f}MB")
            best["combination"] = index_combination
            best["benefit_to_size_ratio"] = ratio
            best["cost"] = cost

        # todo: newly added.
        return ratio
