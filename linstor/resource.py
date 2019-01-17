"""
Resource module
"""

import linstor
import socket
from functools import wraps
from linstor.sharedconsts import FAIL_EXISTS_RSC, FLAG_DISKLESS


class _Client(object):
    def __init__(self, uris, timeout=300, keep_alive=False):
        # external properties
        self.uri_list = linstor.MultiLinstor.controller_uri_list(uris)  # type: list[str]
        self.timeout = timeout
        self.keep_alive = keep_alive


class _Placement(object):
    def __init__(self, redundancy=2):
        self.redundancy = redundancy
        self.storage_pool = None
        self.diskless_storage_pool = None
        self.diskless_on_remaining = False


class Volume(object):
    """
    Volume class represents a DRBD Volume.

    This object contains important properties of a Volume, including
    e.g: ``size``, and ``device_path``

    :param size: String parsable by linstor.SizeCalc or size in bytes.
    """
    def __init__(self, size):
        # external properties
        self._size = self._size_to_bytes(size)
        self._minor = None
        self._backing_disk = ''
        self._device_path = ''
        self._storage_pool_name = ''

        # internal
        self._volume_id = None
        self._rsc_name = None
        self._client_ref = None
        self._assignments = []

    @property
    def storage_pool_name(self):
        return self._storage_pool_name

    @property
    def backing_disk(self):
        """
        Returns the backing disk (e.g., /dev/drbdpool/foo_0001) of a Volume.

        :return: The backing disk of a Volume.
        :rtype: str
        """
        return self._backing_disk

    @backing_disk.setter
    def backing_disk(self, backing_disk):
        if self._rsc_name is not None:
            raise linstor.LinstorReadOnlyAfterSetError()
        self._backing_disk = backing_disk

    @property
    def device_path(self):
        """
        Returns the device path of a Volume (e.g., /dev/drbd1000).

        :return: The device path of a Volume.
        :rtype: str
        """
        return self._device_path

    @device_path.setter
    def device_path(self, device_path):
        raise linstor.LinstorReadOnlyAfterSetError('This is a read-only property')

    @property
    def minor(self):
        """
        Returns the minor number of a Volume (e.g., 1000).

        :return: The minor number of a Volume.
        :rtype: int
        """
        return self._minor

    @minor.setter
    def minor(self, minor):
        if self._rsc_name is not None:
            raise linstor.LinstorReadOnlyAfterSetError()
        self._minor = minor

    @property
    def size(self):
        """
        Returns the size of a Volume (e.g., 1000000).

        Setting tye size of a volume that is deployed triggers a resize operation.

        :return: The size of a Volume in bytes.
        :rtype: int
        """
        return self._size

    @classmethod
    def _size_to_bytes(cls, size):
        if isinstance(size, str):
            return linstor.SizeCalc.auto_convert(size, linstor.SizeCalc.UNIT_B)
        return size

    @size.setter
    def size(self, size):  # this has to be an int, otherwise python complains
        size = self._size_to_bytes(size)
        if self._size is not None \
           and self._rsc_name is not None \
           and self._volume_id is not None:
            r, v = self._rsc_name, self._volume_id
            if self._size > size:
                raise ValueError('shrinking Resource/Volume {}/{}i from {} to {} is not allowed'
                                 .format(r, v, self._size, size))

            size_kib = linstor.SizeCalc.convert_round_up(size, linstor.SizeCalc.UNIT_B,
                                                         linstor.SizeCalc.UNIT_KiB)
            with linstor.MultiLinstor(self._client_ref.uri_list,
                                      self._client_ref.timeout,
                                      self._client_ref.keep_alive) as lin:
                rs = lin.volume_dfn_modify(r, v, size=size_kib)
                if not rs[0].is_success():
                    raise linstor.LinstorError('Could not resize Resource/Volume {}/{}: {}'
                                               .format(r, v, rs[0]))

        # if we are here everyting is fine
        self._size = size

    # called from VolumeDict
    def _delete(self):
        if self._rsc_name is None:  # this volume was created, but never deployed, no linstor action.
            return
        with linstor.MultiLinstor(self._client_ref.uri_list,
                                  self._client_ref.timeout,
                                  self._client_ref.keep_alive) as lin:
            r, v = self._rsc_name, self._volume_id
            rs = lin.volume_dfn_delete(r, v)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not delete Resource/Volume {}/{}: {}'.format(r, v, rs[0]))


class _VolumeDict(dict):
    def __init__(self):
        super(_VolumeDict, self).__init__()

    def __setitem__(self, k, v):
        if not isinstance(v, Volume):
            raise ValueError('{} is not of an instance of Volume'.format(v))
        v._volume_id = k
        super(_VolumeDict, self).__setitem__(k, v)

    def __delitem__(self, k):
        self[k]._delete()
        super(_VolumeDict, self).__delitem__(k)


class Resource(object):
    """
    Resource class represents a DRBD Resource.

    This object allows managing existing DRBD Resources as well as creating new ones.

    :param str name: The name of the DRBD resource.
    :param str uri: A list of controller addresses.
     e.g: ``linstor://localhost,10.0.0.2``, ``linstor+ssl://localhost,linstor://192.168.0.1``
    """
    def _update_volumes(f):
        @wraps(f)
        def wrapper(self, *args, **kwargs):
            ret = None
            with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
                self._lin = lin
                self._maybe_create_rd()
                ret = f(self, *args, **kwargs)
                self.__update_volumes()
            self._lin = None
            return ret
        return wrapper

    def __init__(self, name, uri='linstor://localhost'):
        # external properties
        self._name = name
        self._port = None
        self.client = _Client(uri)
        self.placement = _Placement()
        self.volumes = _VolumeDict()  # type: dict[int, Volume]
        self.defined = False

        # THINK(rck): maybe a dict, KISS for now
        self._allow_two_primaries = False

        # internal
        self._lin = None  # used to pass in an existing client (e.g,, _update_volumes)
        self._assignments = {}

        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            self._lin = lin
            self.__update_volumes()

    def __str__(self):
        return self._name

    def _set_properties(self):
        dp = 'yes' if self._allow_two_primaries else 'no'
        props = {'DrbdOptions/Net/allow-two-primaries': dp}
        rs = self._lin.resource_dfn_modify(self._name, props, delete_props=None)
        if not rs[0].is_success():
            raise linstor.LinstorError('Could not set DRBD properties for resource {}: {}'
                                       .format(self._name, rs[0]))

    def _maybe_create_rd(self):
        if not self.defined:
            rs = self._lin.resource_dfn_create(self._name, self._port)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not create resource definition {}: {}'
                                           .format(self._name, rs[0]))
            self.defined = True
            self._set_properties()

    def __update_volumes(self):
        # create fresh volume definitions
        for k, v in self.volumes.items():
            if v._rsc_name is None:
                size_kib = linstor.SizeCalc.convert_round_up(v.size, linstor.SizeCalc.UNIT_B,
                                                             linstor.SizeCalc.UNIT_KiB)
                rs = self._lin.volume_dfn_create(self._name, size_kib, k, v._minor,
                                                 encrypt=False, storage_pool=self.placement.storage_pool)
                if not rs[0].is_success():
                    raise linstor.LinstorError('Could not create volume definition {}/{}: {}'
                                               .format(self._name, k, rs[0]))
                self.volumes[k]._rsc_name = self._name

        # update internal state
        rsc_dfn_list_replies = self._lin.resource_dfn_list()
        if not rsc_dfn_list_replies or not rsc_dfn_list_replies[0]:
            return True

        rsc_dfn_list_reply = rsc_dfn_list_replies[0]
        for rsc_dfn in rsc_dfn_list_reply.proto_msg.rsc_dfns:
            if rsc_dfn.rsc_name == self._name:
                self.defined = True
                for vlm_dfn in rsc_dfn.vlm_dfns:
                    vlm_nr = vlm_dfn.vlm_nr
                    if not self.volumes.get(vlm_nr):
                        self.volumes[vlm_nr] = Volume(None)
                    self.volumes[vlm_nr]._volume_id = vlm_nr
                    self.volumes[vlm_nr]._rsc_name = self._name
                    self.volumes[vlm_nr]._client_ref = self.client
                    size_b = linstor.SizeCalc.convert_round_up(vlm_dfn.vlm_size, linstor.SizeCalc.UNIT_KiB,
                                                               linstor.SizeCalc.UNIT_B)
                    self.volumes[vlm_nr]._size = size_b
                    self.volumes[vlm_nr]._minor = vlm_dfn.vlm_minor
                for prop in rsc_dfn.rsc_dfn_props:
                    if prop.key == 'DrbdOptions/Net/allow-two-primaries':
                        self._allow_two_primaries = True if prop.value == 'yes' else False

        rsc_list_replies = self._lin.resource_list(filter_by_nodes=None, filter_by_resources=[self._name])
        if not rsc_list_replies or not rsc_list_replies[0]:
            return True

        self._assignments = {}
        rsc_list_reply = rsc_list_replies[0]
        for rsc in rsc_list_reply.proto_msg.resources:
            is_diskless = (FLAG_DISKLESS in rsc.rsc_flags)
            node_name = rsc.node_name
            self._assignments[node_name] = is_diskless
            for vlm in rsc.vlms:
                vlm_nr = vlm.vlm_nr
                self.volumes[vlm_nr]._backing_disk = vlm.backing_disk
                self.volumes[vlm_nr]._device_path = vlm.device_path
                self.volumes[vlm_nr]._storage_pool_name = vlm.stor_pool_name
                self.volumes[vlm_nr]._minor = vlm.vlm_minor_nr

        return True

    @property
    def allow_two_primaries(self):
        """
        Returns the value of the DRBD net-option 'allow-two-primaries'.

        :return: The value of the DRBD net-option 'allow-two-primaries'. Raises LinstorError in case of error.
        :rtype: bool
        """
        return self._allow_two_primaries

    @allow_two_primaries.setter
    def allow_two_primaries(self, value):
        if self._allow_two_primaries == value:
            return

        self._allow_two_primaries = value
        if self.defined:
            with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
                self._lin = lin
                self._set_properties()

    @property
    def name(self):
        """
        Returns the name of the Resource.

        :return: The name of the Resource.
        :rtype: str
        """
        return self._name

    @name.setter
    def name(self, name):
        if self.defined:
            raise linstor.LinstorReadOnlyAfterSetError()
        self._name = name

    @property
    def port(self):
        """
        Returns the port of the Resource.

        :return: The port of the Resource.
        :rtype: str
        """
        return self._port

    @port.setter
    def port(self, port_nr):
        if self.defined:
            raise linstor.LinstorReadOnlyAfterSetError()
        self._port = port_nr

    @_update_volumes
    def autoplace(self):
        """
        Automatically place the Resource according to values set in the placement policy.

        To autoplace a Resource 'foo' 3 times redundant on the storage pool 'drbdpool' one would:
        foo.placement.redundancy = 3
        foo.placement.storage_pool = 'drbdpool'
        foo.autoplace()

        :return: True if success, else raises LinstorError
        """
        rs = self._lin.resource_auto_place(
            self._name,
            self.placement.redundancy,
            self.placement.storage_pool,
            do_not_place_with=None,
            do_not_place_with_regex=None,
            replicas_on_same=None,
            replicas_on_different=None,
            diskless_on_remaining=self.placement.diskless_on_remaining)

        if not rs[0].is_success():
            raise linstor.LinstorError('Could not autoplace resource {}: {}'
                                       .format(self._name, rs[0]))
        return True

    @_update_volumes
    def activate(self, node_name):
        """
        Makes a resource available at a given host.

        If the host already contains a diskful assignment, this is a NOOP. Otherwise a diskless assignment is
        created.

        :return: True if success, else raises LinstorError
        """
        rsc_create_replies = self._lin.resource_create([
            linstor.ResourceData(
                node_name,
                self._name,
                diskless=True
            )
        ])
        rsc_create_reply = rsc_create_replies[0]
        if rsc_create_reply.is_success() or rsc_create_reply.is_error(code=FAIL_EXISTS_RSC):
            return True

        raise linstor.LinstorError('Could not activate resource {} on node {}: {}'
                                   .format(self._name, node_name, rsc_create_reply))

    # no decorator, calles delete
    def deactivate(self, node_name):
        """
        Deactivates a resource on a host if possible.

        If the assignment is diskless, delete this assignment. If it is diskful and therefore part of the
        given redundany, this is a NOOP (i.e., the redundancy is not decreased).

        :return: True if success, else raises LinstorError
        """
        if self.is_diskless(node_name):
            return self.delete(node_name)
        return True

    @_update_volumes
    def _create_or_toggle(self, node_name, diskless):
        is_assigned = self.is_assigned(node_name)
        is_diskless = self.is_diskless(node_name)
        sp = self.placement.diskless_storage_pool
        if is_diskless or (not is_assigned and not diskless):
            sp = self.placement.storage_pool

        if not is_assigned:
            rs = self._lin.resource_create([
                linstor.ResourceData(
                    node_name,
                    self._name,
                    diskless=diskless,
                    storage_pool=sp
                )
            ])
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not create resource {} on node {} as diskless={}: {}'
                                           .format(self._name, node_name, diskless, rs[0]))
        elif is_diskless != diskless:
            rs = self._lin.resource_toggle_disk(node_name, self._name, diskless=diskless, storage_pool=sp)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not toggle disk for resource {} on node {} to diskless={}: {}'
                                           .format(self._name, node_name, diskless, rs[0]))
        return True

    def snapshot_create(self, name):
        """
        Creates a new snapshot for the resource.

        :param str name: Name of the snapshot
        :return: True if success, else raises LinstorError
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            rs = lin.snapshot_create(node_names=[], rsc_name=self.name, snapshot_name=name, async_msg=False)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not create snapshot {}: {}'.format(name, rs[0].message))
        return True

    def snapshot_delete(self, name):
        """
        Deletes a given snapshot of this resource.

        :param str name: Name of the snapshot
        :return: True if success, else raises LinstorError
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            rs = lin.snapshot_delete(rsc_name=self.name, snapshot_name=name)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not delete snapshot {}: {}'.format(name, rs[0].message))
        return True

    def snapshot_rollback(self, name):
        """
        Rolls resource data back to snapshot state. The resource must not be in use.
        The snapshot will not be removed and can be used for subsequent rollbacks.
        Only the most recent snapshot may be used; to roll back to an earlier
        snapshot, the intermediate snapshots must first be deleted.

        :param str name: Name of the snapshot
        :return: True if success, else raises LinstorError
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            rs = lin.snapshot_rollback(rsc_name=self.name, snapshot_name=name)
            if not rs[0].is_success():
                raise linstor.LinstorError('Could not rollback to snapshot {}: {}'.format(name, rs[0].message))
        return True

    def restore_from_snapshot(self, snapshot_name, resource_name_to):
        """
        Restores a new resource from a snapshot.

        :param snapshot_name: Snapshot name to use for restoration.
        :param resource_name_to: Name of the new resource.
        :return: A new resource object restored from the snapshot.
        :rtype: Resource
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            rs = lin.resource_dfn_create(resource_name_to)
            if not rs[0].is_success():
                raise linstor.LinstorError("Could not resource definition '{}' for snapshot restore"
                                           .format(resource_name_to))

            rs = lin.snapshot_volume_definition_restore(
                from_resource=self.name,
                from_snapshot=snapshot_name,
                to_resource=resource_name_to
            )

            if not rs[0].is_success():
                raise linstor.LinstorError(
                    "Could not restore volume definition '{rd}' from snapshot {sn} to resource definition '{tr}'"
                    .format(rd=self.name, sn=snapshot_name, tr=resource_name_to)
                )

            rs = lin.snapshot_resource_restore(
                node_names=[],  # to all
                from_resource=self.name,
                from_snapshot=snapshot_name,
                to_resource=resource_name_to
            )

            if not rs[0].is_success():
                raise linstor.LinstorError(
                    "Could not restore resource '{rd}' from snapshot {sn} to resource definition '{tr}'"
                    .format(rd=self.name, sn=snapshot_name, tr=resource_name_to)
                )

        return Resource(resource_name_to, ",".join(self.client.uri_list))

    def diskless(self, node_name):
        """
        Assign a resource diskless on a given node.

        If the assignment does not exist, create it diskless. If the assignment is already diskless, this is a
        NOOP. If it exists diskful, convert it to diskless.

        :param str node_name: Name of the node
        :return: True if success, else raises LinstorError
        """
        return self._create_or_toggle(node_name, True)

    def diskful(self, node_name):
        """
        Assign a resource diskful on a given node.

        If the assignment does not exist, create it diskful. If the assignment is already diskful, this is a
        NOOP. If it exists diskless, convert it to diskful.

        :param str node_name: Name of the node
        :return: True if success, else raises LinstorError
        """
        return self._create_or_toggle(node_name, False)

    def is_diskless(self, node_name):
        """
        Returns True if the resource is assigned diskless on the given host.

        :param str node_name: Name of the node
        :return: True if assigned diskless on given host.
        :rtype: bool
        """
        return self._assignments.get(node_name, False)

    def is_diskful(self, node_name):
        """
        Returns True if the resource is assigned diskful on the given host.

        :param str node_name: Name of the node
        :return: True if assigned diskful on given host.
        :rtype: bool
        """
        return not self._assignments.get(node_name, True)

    def is_assigned(self, node_name):
        """
        Returns True if the resource is assigned diskful or diskless on the given host.

        :param str node_name: Name of the node
        :return: True if assigned (diskful or diskless) on given host.
        :rtype: bool
        """
        return self.is_diskful(node_name) or self.is_diskless(node_name)

    def diskless_nodes(self):
        """
        Returns the host names of all diskless nodes.

        :return: Host names of diskless nodes.
        :rtype: list[str]
        """
        return [n for n in self._assignments.keys() if self.is_diskless(n)]

    def diskful_nodes(self):
        """
        Returns the host names of all diskful nodes.

        :return: Host names of diskful nodes.
        :rtype: list[str]
        """
        return [n for n in self._assignments.keys() if self.is_diskful(n)]

    def is_thin(self):
        """
        Returns if the used storage pool of the resource is thin.

        :return: True if storage pool used is thin.
        :rtype: bool
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            stor_pool_list = lin.storage_pool_list_raise(None, filter_by_stor_pools=[self.volumes[0].storage_pool_name])
            return stor_pool_list.storage_pools[0].is_thin()

    # no decorator! (could recreate)
    def _delete(self, node_name=None):
        rs = None
        reinit = False
        if node_name is None:
            node_name = 'ALL'  # for error msg
            rs = self._lin.resource_dfn_delete(self._name)
            reinit = True
            self.defined = False
        else:
            if not self.is_assigned(node_name):
                return True
            rs = self._lin.resource_delete(node_name, self._name)
            if socket.gethostname() == node_name:  # deleted on myself
                reinit = True

        if not rs[0].is_success():
            raise linstor.LinstorError('Could not delete resource {} on node {}: {}'
                                       .format(self._name, node_name, rs[0]))
        if reinit:
            self._volumes = _VolumeDict()

        return self.__update_volumes()

    # no decorator! (could recreate)
    def delete(self, node_name=None, snapshots=True):
        """
        Deletes the resource globally or on the given host.

        If the node name is None, deletes the resource globally.

        :param str node_name: Deletes resource only from the specified node.
        :param bool snapshots: If True deletes snapshots prior deleting the resource

        :return: True if success, else raises LinstorError
        """
        with linstor.MultiLinstor(self.client.uri_list, self.client.timeout, self.client.keep_alive) as lin:
            self._lin = lin

            if snapshots:
                snapshot_list = lin.snapshot_dfn_list()[0]
                for snap in [x for x in snapshot_list.proto_msg.snapshot_dfns if x.rsc_name == self._name]:
                    lin.snapshot_delete(rsc_name=self._name, snapshot_name=snap.snapshot_name)

            return self._delete(node_name)
