from typing import Union, List, Tuple
import re
import numpy
import tensorflow.contrib.graph_editor as ge
from tensorflow.contrib.graph_editor.util import ListView

from neuralmagicML.tensorflow.utils.helpers import tf_compat

__all__ = [
    "VAR_INDEX_FROM_TRAINABLE",
    "get_op_var_index",
    "clean_tensor_name",
    "get_op_input_var",
    "get_tensor_var",
    "get_prunable_ops",
    "get_ops_and_inputs_by_name_or_regex",
    "any_str_or_regex_matches_tensor_name",
    "eval_tensor_density",
    "eval_tensor_sparsity",
]


VAR_INDEX_FROM_TRAINABLE = "from_trainable"


def get_op_var_index(var_index: Union[str, int], op_inputs: ListView) -> int:
    """
    Get the index of the variable input to an operation.
    Ex: getting the index of the weight for a convolutional operator.

    | There are a few different modes that this can work as for var_index value:
    |   - int given, treats this as the desired index of the weight
    |   - string given equal to VAR_INDEX_FROM_TRAINABLE, picks the most likely input
    |     based on finding the first trainable variable that is an input.
    |     Defaults to the last input (all convs have input as last and most matmuls)
    |   - string given, attempts to match the string in any of the inputs name.
    |     Uses the first one found, raises an exception if one couldn't be found

    :param var_index: the index to use for figuring out the proper input
    :param op_inputs: inputs to the operator from graph editor
    :return: the integer representing the index of the desired variable
    """
    # check if given index is explicit
    if isinstance(var_index, int):
        if var_index < 0:
            var_index += len(op_inputs)

        return var_index

    # check through trainable vars for best fit
    if var_index == "from_trainable":
        # default to last as this is where tf by default is configured
        # to put the weight variables
        weight_index = len(op_inputs) - 1
        trainable_vars = [var.name for var in tf_compat.trainable_variables()]

        for index, inp in enumerate(op_inputs):
            expected_name = "{}:0".format(clean_tensor_name(inp.name))

            if expected_name in trainable_vars:
                return index

        return weight_index

    # assume that the passed in value is an identifier for the variable name
    for index, inp in enumerate(op_inputs):
        if var_index in inp.name:
            return index

    raise ValueError("unknown value given for var_index of {}".format(var_index))


def clean_tensor_name(var_tens: Union[str, tf_compat.Tensor]) -> str:
    """
    :param var_tens: the tensor to get a variable for
    :return: the cleaned version of the name for a variable tensor
        (removes read and indices at the end)
    """
    name = var_tens if isinstance(var_tens, str) else var_tens.name
    name = re.sub(r"/read:[0-9]+$", "", name)
    name = re.sub(r":[0-9]+$", "", name)

    return name


def get_op_input_var(
    operation: tf_compat.Operation,
    var_index: Union[str, int] = VAR_INDEX_FROM_TRAINABLE,
) -> tf_compat.Tensor:
    """
    Get the input variable for an operation.
    Ex: the weight for a conv operator.
    See @get_op_var_index for proper values for var_index.

    :param operation: the operation to get the input variable for
    :param var_index: the index to guide which input to grab from the operation
    :return: the tensor input that represents the variable input for the operation
    """
    op_sgv = ge.sgv(operation)
    var_index = get_op_var_index(var_index, op_sgv.inputs)

    return op_sgv.inputs[var_index]


def get_tensor_var(tens: tf_compat.Tensor) -> tf_compat.Variable:
    """
    Get the variable associated with a given tensor.
    Raises a ValueError if not found

    :param tens: the tensor to find a variable for
    :return: the found variable matching the given tensor
    """
    expected_name = "{}:0".format(clean_tensor_name(tens))

    for var in tf_compat.global_variables():
        if expected_name == var.name:
            return var

    raise ValueError(
        "could not find a global variable that matched the tensor {}".format(tens)
    )


def get_prunable_ops(
    graph: tf_compat.Graph = None,
) -> List[Tuple[str, tf_compat.Operation]]:
    """
    Get the prunable operations from a TensorFlow graph.

    :param graph: the graph to get the prunable operations from.
        If not supplied, then will use the default graph
    :return: a list containing the names and ops of the prunable operations
        (MatMul, Conv1D, Conv2D, Conv3D)
    """
    if not graph:
        graph = tf_compat.get_default_graph()

    ops = []

    for op in graph.get_operations():
        if (
            op.type in ["MatMul", "Conv1D", "Conv2D", "Conv3D", "DepthwiseConv2dNative"]
            and "gradients/" not in op.name
            and "_grad/" not in op.name
        ):
            ops.append((op.name, op))

    return ops


def get_ops_and_inputs_by_name_or_regex(
    var_names: List[str], graph: tf_compat.Graph = None,
) -> List[Tuple[tf_compat.Operation, tf_compat.Tensor]]:
    """
    Get tuples of operations and the inputs for inputs of operations that match
    a regex pattern in the list params.

    :param var_names: List of full names or regex patterns to match variable names by.
    :param graph: the graph to get the prunable operations from.
        If not supplied, then will use the default graph
    :return: a list of (operation, parameter) pairs for parameters that match a
        regex pattern in var_names.  If the wildcards '.' or '.*' are provided as regex
        patterns, then will match on all prunable layers and return variables using
        get_op_input_var
    """
    prunable_ops_and_inputs = []
    if "re:.*" in var_names or "re:." in var_names:  # wildcard cases
        ops = get_prunable_ops(graph)
        for _, op in ops:
            op_sgv = ge.sgv(op)
            for inp in op_sgv.inputs:
                prunable_ops_and_inputs.append((op, get_op_input_var(op)))
    else:
        for var in tf_compat.global_variables():
            if any_str_or_regex_matches_tensor_name(var.name, var_names):
                var_tens = graph.get_tensor_by_name(var.name)
                # get all the read ops for the var
                read_ops = [
                    read_op
                    for read_op in ge.get_consuming_ops(var_tens)
                    if "/read" == read_op.name[-5:]
                ]  # filter for /read ops
                read_tensors = {
                    read_tensor
                    for read_op in read_ops
                    for read_tensor in ge.sgv(read_op).outputs
                }
                # gets ops that read from read_tensors and filters any ops that were created by mask_ks
                consuming_ops_with_input = [
                    (consuming_op, read_tensor)
                    for read_tensor in read_tensors
                    for consuming_op in ge.get_consuming_ops(read_tensor)
                ]
                for op, inp in consuming_ops_with_input:
                    if "_nm_ks" not in op.name:
                        prunable_ops_and_inputs.append((op, inp))
                    else:
                        nm_ks_consuming_ops_with_input = [
                            (consuming_op, inp)
                            for output_tens in ge.sgv(op).outputs
                            for consuming_op in ge.get_consuming_ops(output_tens)
                            if "_nm_ks" not in consuming_op.name
                        ]
                        prunable_ops_and_inputs += nm_ks_consuming_ops_with_input
    # Check that all var_names values have a match
    _validate_all_params_found(var_names, prunable_ops_and_inputs)
    return prunable_ops_and_inputs


def any_str_or_regex_matches_tensor_name(
    tensor_name: str, name_or_regex_patterns: List[str],
):
    """
    :param tensor_name: The name of a tensor
    :param name_or_regex_patterns: List of full tensor names to match to the input or
        regex patterns to match with that should be prefixed with 're:'
    :return: True if any given str or regex pattern matches the given name
    """
    clean_name = clean_tensor_name(tensor_name)
    for name_or_regex in name_or_regex_patterns:
        if name_or_regex[:3] == "re:":
            pattern = name_or_regex[3:]
            if re.match(pattern, tensor_name) or re.match(pattern, clean_name):
                return True
        else:
            if tensor_name == name_or_regex or clean_name == name_or_regex:
                return True
    return False


def _validate_all_params_found(
    name_or_regex_patterns: List[str],
    prunable_ops_and_inputs: List[Tuple[tf_compat.Operation, tf_compat.Tensor]],
):
    """
    :param name_or_regex_patterns: List of full param names or regex patterns of them
        to check for matches in named_layers_and_params names
    :param prunable_ops_and_inputs: List prunable ops and inputs found in
        get_ops_and_inputs_by_name_or_regex
    :raise RuntimeError: If there is a name or regex pattern that does not have a
        match in named_layers_and_params
    """
    tensor_names = [inp.name for _, inp in prunable_ops_and_inputs]
    for name_or_regex in name_or_regex_patterns:
        # Convert all name_or_regex values to regex patterns since we may want
        # full names to match based on tensor name extensions
        pattern = name_or_regex if name_or_regex[:3] != "re:" else name_or_regex[3:]

        if any(re.match(pattern, name) for name in tensor_names):
            continue  # regex pattern matches at least one full parameter name

        raise RuntimeError(
            "All supplied parameter names or regex patterns not found."
            "No match for {} in found tensors {}.  Supplied {}".format(
                name_or_regex, tensor_names, name_or_regex_patterns
            )
        )


def eval_tensor_density(
    tens: tf_compat.Tensor, sess: tf_compat.Session = None
) -> float:
    """
    Get the density (fraction of non zero values) in a tensor

    :param tens: the tensor to get the density for
    :param sess: the session to use for evaluating the tensor,
        if not supplied will use the default session
    :return: the density of the tensor
    """
    if not sess:
        sess = tf_compat.get_default_session()

    val_array = sess.run(tens)
    num_nonzeros = numpy.count_nonzero(val_array)
    density = float(num_nonzeros) / float(val_array.size)

    return density


def eval_tensor_sparsity(
    tens: tf_compat.Tensor, sess: tf_compat.Session = None
) -> float:
    """
    Get the sparsity (fraction of zero values) in a tensor

    :param tens: the tensor to get the sparsity for
    :param sess: the session to use for evaluating the tensor,
        if not supplied will use the default session
    :return: the sparsity of the tensor
    """
    return 1.0 - eval_tensor_density(tens, sess)
