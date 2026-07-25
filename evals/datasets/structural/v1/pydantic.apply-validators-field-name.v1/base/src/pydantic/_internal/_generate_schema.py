# TODO V3: this function is only used for deprecated decorators. It should
# be removed once we drop support for those.
def apply_validators(
    schema: core_schema.CoreSchema,
    validators: Iterable[Decorator[RootValidatorDecoratorInfo]]
    | Iterable[Decorator[ValidatorDecoratorInfo]]
    | Iterable[Decorator[FieldValidatorDecoratorInfo]],
) -> core_schema.CoreSchema:
    """Apply validators to a schema.

    Args:
        schema: The schema to apply validators on.
        validators: An iterable of validators.
        field_name: The name of the field if validators are being applied to a model field.

    Returns:
        The updated schema.
    """
    for validator in validators:
        # Actually, type could be 'field' or 'model', but this is only used for deprecated
        # decorators, so let's not worry about it.
        info_arg = inspect_validator(validator.func, mode=validator.info.mode, type='field')
        val_type = 'with-info' if info_arg else 'no-info'

        schema = _VALIDATOR_F_MATCH[(validator.info.mode, val_type)](validator.func, schema)
    return schema
