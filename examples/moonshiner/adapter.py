"""Pin this wrapper in the existing project's reviewed source closure."""
from modelbuilder.backend.components.moonshiner import prepare_dataset


def run(*, inputs, parameters, context):
    return prepare_dataset(inputs=inputs, parameters=parameters, context=context)
