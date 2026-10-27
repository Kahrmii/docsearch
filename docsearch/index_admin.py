"""Deleting an index: validation, the warning text and the clean-up that belongs to it.

Shared by the window and the web interface. Deleting is permanent, so it needs the typed index name."""
from . import cloud_policy
from .es import valid_index_name


class IndexNotFound(LookupError):
    pass


def describe(es, index):
    """Facts for the warning: documents, size and cloud approval.

    Raises ValueError for an invalid name and IndexNotFound if the index does not exist."""
    if not valid_index_name(index):
        raise ValueError("Index name: only lowercase letters, digits, _ and -.")
    info = es.index_info(index)
    if info is None:
        raise IndexNotFound(index)
    return {**info, "cloud_allowed": cloud_policy.is_allowed(index)}


def warning_text(index, info):
    lines = [f"Permanently delete the index '{index}'?", "",
             f"It holds {info['documents']} documents ({info['size_mb']} MB).",
             "This cannot be undone.",
             "Your original files on disk are not touched, but to get the index back you have to index the "
             "folder again, which can take hours for large collections."]
    if info["cloud_allowed"]:
        lines.append("Its approval for the cloud is removed as well.")
    return "\n".join(lines)


def delete(es, index, confirm_name):
    """Delete the index after the user typed its name. Returns the facts of the deleted index.

    Raises ValueError if the typed name does not match (nothing is deleted)."""
    if confirm_name != index:
        raise ValueError("The typed name does not match the index name; nothing was deleted.")
    info = describe(es, index)
    es.delete_index(index)
    cloud_policy.set_allowed(index, False)  # a new index with this name must not inherit the approval
    return info
