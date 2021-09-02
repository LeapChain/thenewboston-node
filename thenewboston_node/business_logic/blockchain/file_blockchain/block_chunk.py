import logging
import os.path
import re
from collections import namedtuple
from typing import Generator, Optional

import msgpack
from more_itertools import always_reversible

from thenewboston_node.business_logic.exceptions import InvalidBlockchainError
from thenewboston_node.business_logic.models.block import Block
from thenewboston_node.business_logic.storages.file_system import COMPRESSION_FUNCTIONS
from thenewboston_node.core.logging import timeit
from thenewboston_node.core.utils.file_lock import ensure_locked, lock_method

from .base import EXPECTED_LOCK_EXCEPTION, LOCKED_EXCEPTION, FileBlockchainBaseMixin  # noqa: I101

BLOCK_CHUNK_FILENAME_TEMPLATE = '{start}-{end}-block-chunk.msgpack'
BLOCK_CHUNK_FILENAME_RE = re.compile(
    BLOCK_CHUNK_FILENAME_TEMPLATE.format(start=r'(?P<start>\d+)', end=r'(?P<end>\d+|x+)') +
    r'(?:|\.(?P<compression>{}))$'.format('|'.join(COMPRESSION_FUNCTIONS.keys()))
)

BlockChunkFilenameMeta = namedtuple('BlockChunkFilenameMeta', 'start end compression')

logger = logging.getLogger(__name__)


def get_block_chunk_filename_meta(filename):
    match = BLOCK_CHUNK_FILENAME_RE.match(filename)
    if match:
        start = int(match.group('start'))
        end_str = match.group('end')
        if ''.join(set(end_str)) == 'x':
            end = None
        else:
            end = int(end_str)
            assert start <= end

        return BlockChunkFilenameMeta(start, end, match.group('compression') or None)

    return None


def get_block_chunk_file_path_meta(file_path):
    return get_block_chunk_filename_meta(os.path.basename(file_path))


class BlockChunkFileBlockchainMixin(FileBlockchainBaseMixin):

    def get_block_chunk_subdirectory(self) -> str:
        raise NotImplementedError('Must be implemented in child class')

    def get_block_chunk_last_block_number_cache(self):
        raise NotImplementedError('Must be implemented in child class')

    def get_block_chunk_storage(self):
        raise NotImplementedError('Must be implemented in child class')

    def get_block_cache(self):
        raise NotImplementedError('Must be implemented in child class')

    @staticmethod
    def make_block_chunk_filename_from_start_end_str(start_block_str, end_block_str):
        return BLOCK_CHUNK_FILENAME_TEMPLATE.format(start=start_block_str, end=end_block_str)

    def make_block_chunk_filename_from_start_end(self, start_block, end_block=None):
        block_number_digits_count = self.get_block_number_digits_count()
        start_block_str = str(start_block).zfill(block_number_digits_count)
        if end_block is None:
            end_block_str = 'x' * block_number_digits_count
        else:
            end_block_str = str(end_block).zfill(block_number_digits_count)

        return BLOCK_CHUNK_FILENAME_TEMPLATE.format(start=start_block_str, end=end_block_str)

    def _get_block_chunk_last_block_number(self, file_path):
        key = self._make_block_chunk_last_block_number_cache_key(file_path)
        block_chunk_last_block_number_cache = self.get_block_chunk_last_block_number_cache()
        last_block_number = block_chunk_last_block_number_cache.get(key)
        if last_block_number is not None:
            return last_block_number

        for block in self._yield_blocks_from_file(file_path, -1):
            block_chunk_last_block_number_cache[key] = last_block_number = block.get_block_number()
            break

        return last_block_number

    def _get_block_chunk_file_path_meta_enhanced(self, file_path):
        meta = get_block_chunk_file_path_meta(file_path)
        if meta is None:
            return None

        if meta.end is not None:
            return meta

        return meta._replace(end=self._get_block_chunk_last_block_number(file_path))

    def _make_last_block_chunk_file_path_key(self):
        return self._make_block_chunk_last_block_number_cache_key(self._get_last_block_chunk_file_path())

    def _make_block_chunk_last_block_number_cache_key(self, file_path):
        return file_path, self.get_block_chunk_storage().get_mtime(file_path)

    @lock_method(lock_attr='file_lock', exception=LOCKED_EXCEPTION)
    def add_block(self, block: Block, validate=True):
        block_number = block.get_block_number()

        logger.debug('Adding block number %s to the blockchain', block_number)
        rv = super().add_block(block, validate)  # type: ignore

        cache = self.get_block_chunk_last_block_number_cache()
        assert cache is not None

        key = self._make_last_block_chunk_file_path_key()
        cache[key] = block_number

        return rv

    def make_block_chunk_filename(self, start_block, end_block=None):
        block_number_digits_count = self.get_block_number_digits_count()
        start_block_str = str(start_block).zfill(block_number_digits_count)
        if end_block is None:
            end_block_str = 'x' * block_number_digits_count
        else:
            end_block_str = str(end_block).zfill(block_number_digits_count)

        return self.make_block_chunk_filename_from_start_end_str(start_block_str, end_block_str)

    def get_current_block_chunk_filename(self) -> str:
        chunk_block_number_start = self.get_last_blockchain_state().last_block_number + 1  # type: ignore
        return self.make_block_chunk_filename(chunk_block_number_start)

    @ensure_locked(lock_attr='file_lock', exception=EXPECTED_LOCK_EXCEPTION)
    def finalize_block_chunk(self, block_chunk_filename, last_block_number=None):
        storage = self.get_block_chunk_storage()
        assert not storage.is_finalized(block_chunk_filename)

        meta = get_block_chunk_filename_meta(block_chunk_filename)
        assert meta.end is None
        if last_block_number is None:
            try:
                block = next(self._yield_blocks_from_file_simple(block_chunk_filename, -1))
            except StopIteration:
                raise InvalidBlockchainError(f'File {block_chunk_filename} does not appear to contain blocks')

            last_block_number = block.get_block_number()

        end = last_block_number
        destination_filename = self.make_block_chunk_filename_from_start_end(meta.start, end)
        storage.move(block_chunk_filename, destination_filename)
        storage.finalize(destination_filename)

        chunk_file_path = storage.get_optimized_actual_path(destination_filename)
        meta = get_block_chunk_file_path_meta(destination_filename)
        end = meta.end
        assert end is not None
        block_cache = self.get_block_cache()
        for block_number in range(meta.start, end + 1):
            block = block_cache.get(block_number)
            if block is None:
                continue

            assert block.meta['chunk_file_path'].endswith(block_chunk_filename)  # type: ignore
            self._set_block_meta(block, meta, chunk_file_path)

    def finalize_all_block_chunks(self):
        # This method is used to clean for super rare case when something goes wrong between blockchain state
        # generation and block chunk finalization

        # TODO(dmu) HIGH: Implemenet a higher performance algorithm for this. Options: 1) cache finalized names
        #                 to avoid filename parsing 2) list directory by glob pattern 3) cache the last known
        #                 finalized name to reduce traversal
        for filename in self.get_block_chunk_storage().list_directory():
            meta = get_block_chunk_filename_meta(filename)
            if meta.end is None:
                logger.warning('Found not finalized block chunk: %s', filename)
                self.finalize_block_chunk(filename)

    @ensure_locked(lock_attr='file_lock', exception=EXPECTED_LOCK_EXCEPTION)
    def persist_block(self, block: Block):
        self.get_block_chunk_storage().append(self.get_current_block_chunk_filename(), block.to_messagepack())

    def yield_blocks(self) -> Generator[Block, None, None]:
        yield from self._yield_blocks(1)

    @timeit(verbose_args=True, is_method=True)
    def yield_blocks_reversed(self) -> Generator[Block, None, None]:
        yield from self._yield_blocks(-1)

    def yield_blocks_from(self, block_number: int) -> Generator[Block, None, None]:
        for file_path in self._list_block_directory():
            meta = self._get_block_chunk_file_path_meta_enhanced(file_path)
            if meta is None:
                logger.warning('File %s has invalid name format', file_path)
                continue

            if meta.end < block_number:
                continue

            yield from self._yield_blocks_from_file_cached(file_path, direction=1, start=max(meta.start, block_number))

    def get_block_by_number(self, block_number: int) -> Optional[Block]:
        blocks_cache = self.get_block_cache()
        assert blocks_cache is not None
        block = blocks_cache.get(block_number)
        if block is not None:
            return block

        try:
            return next(self.yield_blocks_from(block_number))
        except StopIteration:
            return None

    def get_block_count(self) -> int:
        count = 0
        for file_path in self._list_block_directory():
            meta = self._get_block_chunk_file_path_meta_enhanced(file_path)
            if meta is None:
                logger.warning('File %s has invalid name format', file_path)
                continue

            count += meta.end - meta.start + 1

        return count

    def get_next_block_number(self) -> int:
        last_block_chunk_file_path = self._get_last_block_chunk_file_path()
        if last_block_chunk_file_path is None:
            blockchain_state = self.get_last_blockchain_state()  # type: ignore
            assert blockchain_state
            return blockchain_state.next_block_number

        return self._get_block_chunk_last_block_number(last_block_chunk_file_path) + 1

    @timeit(verbose_args=True, is_method=True)
    def _yield_blocks(self, direction) -> Generator[Block, None, None]:
        assert direction in (1, -1)

        for file_path in self._list_block_directory(direction):
            yield from self._yield_blocks_from_file_cached(file_path, direction)

    def _yield_blocks_from_file_cached(self, file_path, direction, start=None):
        assert direction in (1, -1)

        meta = self._get_block_chunk_file_path_meta_enhanced(file_path)
        if meta is None:
            logger.warning('File %s has invalid name fyield_blocks_fromormat', file_path)
            return

        file_start = meta.start
        file_end = meta.end
        if direction == 1:
            next_block_number = cache_start = file_start if start is None else start
            cache_end = file_end
        else:
            cache_start = file_start
            next_block_number = cache_end = file_end if start is None else start

        for block in self._yield_blocks_from_cache(cache_start, cache_end, direction):
            assert next_block_number == block.message.block_number
            next_block_number += direction
            yield block

        if file_start <= next_block_number <= file_end:
            yield from self._yield_blocks_from_file(file_path, direction, start=next_block_number)

    @staticmethod
    def _set_block_meta(block, meta, chunk_file_path):
        block.meta = {
            'chunk_start_block': meta.start,
            'chunk_end_block': meta.end,
            'chunk_compression': meta.compression,
            'chunk_file_path': chunk_file_path
        }

    def _yield_blocks_from_file_simple(self, file_path, direction):
        assert direction in (1, -1)

        storage = self.get_block_chunk_storage()

        unpacker = msgpack.Unpacker()
        unpacker.feed(storage.load(file_path))
        if direction == -1:
            unpacker = always_reversible(unpacker)

        for block_compact_dict in unpacker:
            yield Block.from_compact_dict(block_compact_dict)

    def _yield_blocks_from_file(self, file_path, direction, start=None):
        assert direction in (1, -1)

        chunk_file_path = self.get_block_chunk_storage().get_optimized_actual_path(file_path)
        meta = get_block_chunk_file_path_meta(file_path)

        blocks_cache = self.get_block_cache()
        for block in self._yield_blocks_from_file_simple(file_path, direction):
            block_number = block.get_block_number()
            # TODO(dmu) HIGH: Implement a better skip
            if start is not None:
                if direction == 1 and block_number < start:
                    continue
                elif direction == -1 and block_number > start:
                    continue

            assert block.meta is None
            self._set_block_meta(block, meta, chunk_file_path)

            blocks_cache[block_number] = block
            yield block

    def _yield_blocks_from_cache(self, start_block_number, end_block_number, direction):
        assert direction in (1, -1)

        iter_ = range(start_block_number, end_block_number + 1)
        if direction == -1:
            iter_ = always_reversible(iter_)

        blocks_cache = self.get_block_cache()
        for block_number in iter_:
            block = blocks_cache.get(block_number)
            if block is None:
                break

            yield block

    def _list_block_directory(self, direction=1):
        yield from self.get_block_chunk_storage().list_directory(sort_direction=direction)

    def _get_last_block_chunk_file_path(self):
        try:
            return next(self._list_block_directory(-1))
        except StopIteration:
            return None
