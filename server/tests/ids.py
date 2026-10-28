from uuid import UUID, NAMESPACE_URL, uuid5


def fixture_uuid(label: str) -> str:
    try:
        return str(UUID(label))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, "smartvoiceinput-test:" + label))
