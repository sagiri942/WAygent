"""Frozen MySQL saver 3.0.0 input guard; Runtime adoption belongs to ticket 08.

Validate complete writes before opening a transaction. Limits are Unicode
character counts from the installed 3.0.0 migrations, never payload limits.
"""
from importlib.metadata import version
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver


class CheckpointValidationError(ValueError):
    """A checkpoint identifier cannot be stored losslessly; no input values exposed."""


def _text(field, value, limit):
    if not isinstance(value, str):
        raise CheckpointValidationError(f'{field} must be a string')
    if len(value) > limit:
        raise CheckpointValidationError(f'{field} length {len(value)} exceeds {limit}')
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise CheckpointValidationError(f'{field} contains invalid Unicode')


def _version(value):
    # ChannelVersions publicly supports integers, floats and strings. Preserve
    # the numeric value passed to the upstream driver; only measure SQL text.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CheckpointValidationError('version must be a string or number')
    _text('version', str(value), 150)


class GuardedAIOMySQLSaver(AIOMySQLSaver):
    """Fail-closed guard retaining upstream async saver and transaction behavior."""
    def __init__(self, conn, *, serde=None):
        if version('langgraph-checkpoint-mysql') != '3.0.0':
            raise RuntimeError('checkpoint input guard requires saver 3.0.0')
        super().__init__(conn, serde=serde)

    @staticmethod
    def _validate_config(config, *, parent=False):
        values = config['configurable']
        _text('thread_id', values['thread_id'], 150)
        _text('checkpoint_ns', values['checkpoint_ns'], 2000)
        if 'checkpoint_id' in values:
            if parent and values['checkpoint_id'] is None:
                return
            _text('parent_checkpoint_id' if parent else 'checkpoint_id', values['checkpoint_id'], 150)
        elif not parent:
            raise CheckpointValidationError('checkpoint_id is required')

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._validate_config(config, parent=True)
        _text('checkpoint_id', checkpoint['id'], 150)
        for mapping in (checkpoint['channel_values'], checkpoint['channel_versions'], new_versions):
            for channel in mapping:
                _text('channel', channel, 150)
        for mapping in (checkpoint['channel_versions'], new_versions):
            for value in mapping.values():
                _version(value)
        for channel, value in checkpoint['channel_values'].items():
            if channel in new_versions and value is not None and not isinstance(value, (str, int, float, bool)):
                _text('type', self.serde.dumps_typed(value)[0], 150)
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        self._validate_config(config)
        _text('task_id', task_id, 150)
        _text('task_path', task_path, 2000)
        # Sequence is upstream's documented contract; materializing also avoids
        # iterator consumption during its all(...)/serialization passes.
        batch = list(writes)
        for channel, value in batch:
            _text('channel', channel, 150)
            _text('type', self.serde.dumps_typed(value)[0], 150)
        return await super().aput_writes(config, batch, task_id, task_path)

    def _dump_blobs(self, *args):
        rows = super()._dump_blobs(*args)
        for row in rows:
            _text('type', row[5], 150)
        return rows

    def _dump_writes(self, *args):
        rows = super()._dump_writes(*args)
        for row in rows:
            _text('type', row[8], 150)
        return rows
