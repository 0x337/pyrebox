# -------------------------------------------------------------------------
#
#   Copyright (C) 2017 Cisco Talos Security Intelligence and Research Group
#
#   PyREBox: Python scriptable Reverse Engineering Sandbox
#   Author: Xabier Ugarte-Pedrero
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as
#   published by the Free Software Foundation.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#   MA 02110-1301, USA.
#
# -------------------------------------------------------------------------

from utils import pp_error
import volatility.obj as obj
# import volatility.plugins.kdbgscan as kdbg
import volatility.utils as utils
import traceback

last_kdbg = None

# Modules pending symbol resolution
mods_pending = {}


def windows_insert_module_internal(
        p_pid,
        p_pgd,
        base,
        size,
        fullname,
        basename,
        checksum,
        nt_header,
        update_symbols):

    from utils import get_addr_space
    from vmi import modules
    from vmi import symbols
    from vmi import Module
    from vmi import PseudoLDRDATA

    mod = Module(base, size, p_pid, p_pgd, checksum, basename, fullname)
    if p_pgd != 0:
        addr_space = get_addr_space(p_pgd)
    else:
        addr_space = get_addr_space()

    # Getting symbols, from cache!
    if (checksum, fullname) in symbols:
        mod.set_symbols(symbols[(checksum, fullname)])
    elif update_symbols:
        syms = {}
        export_dir = nt_header.OptionalHeader.DataDirectory[0]
        if export_dir:
            expdir = obj.Object(
                '_IMAGE_EXPORT_DIRECTORY',
                offset=base +
                export_dir.VirtualAddress,
                vm=addr_space,
                parent=PseudoLDRDATA(
                    base,
                    basename,
                    export_dir))
            if expdir.valid(nt_header):
                # Ordinal, Function RVA, and Name Object
                for o, f, n in expdir._exported_functions():
                    if not isinstance(o, obj.NoneObject) and \
                       not isinstance(f, obj.NoneObject) and \
                       not isinstance(n, obj.NoneObject):
                        syms[str(n)] = f.v()
        if len(syms) > 0:
            symbols[(checksum, fullname)] = syms
            mod.set_symbols(syms)
            if (checksum, fullname) in mods_pending:
                for m in mods_pending[(checksum, fullname)]:
                    m.set_symbols(syms)
                del mods_pending[(checksum, fullname)]
        else:
            if (checksum, fullname) in mods_pending:
                mods_pending[(checksum, fullname)].append(mod)
            else:
                mods_pending[(checksum, fullname)] = [mod]

    if base in modules[(p_pid, p_pgd)]:
        del modules[(p_pid, p_pgd)][base]

    modules[(p_pid, p_pgd)][base] = mod


def windows_insert_module(p_pid, p_pgd, module, update_symbols):
    '''
        Insert a module in the module list, only if it has not been inserted yet
    '''

    base = module.DllBase.v()
    if isinstance(base, obj.NoneObject):
        base = 0
    size = module.SizeOfImage.v()
    if isinstance(size, obj.NoneObject):
        size = 0
    fullname = module.FullDllName.v()
    if isinstance(fullname, obj.NoneObject):
        fullname = "Unknown"
    basename = module.BaseDllName.v()
    if isinstance(basename, obj.NoneObject):
        basename = "Unknown"

    # checksum
    nt_header = module._nt_header()
    if not isinstance(nt_header, obj.NoneObject):
        checksum = nt_header.OptionalHeader.CheckSum.v()
        if isinstance(checksum, obj.NoneObject):
            checksum = 0
    else:
        checksum = 0

    windows_insert_module_internal(
        p_pid,
        p_pgd,
        base,
        size,
        fullname,
        basename,
        checksum,
        nt_header,
        update_symbols)


def windows_update_modules(pgd, update_symbols=False):
    '''
        Use volatility to get the modules and symbols for a given process, and
        update the cache accordingly
    '''
    global last_kdbg

    import api
    from utils import get_addr_space
    from vmi import modules

    if pgd != 0:
        addr_space = get_addr_space(pgd)
    else:
        addr_space = get_addr_space()

    if addr_space is None:
        pp_error("Volatility address space not loaded\n")
        return
    # Get EPROC directly from its offset
    procs = api.get_process_list()
    inserted_bases = []
    # Parse/update kernel modules if pgd 0 is requested:
    if pgd == 0 and last_kdbg is not None:
        if (0, 0) not in modules:
            modules[(0, 0)] = {}

        kdbg = obj.Object(
            "_KDDEBUGGER_DATA64",
            offset=last_kdbg,
            vm=addr_space)
        for module in kdbg.modules():
            if module.DllBase not in inserted_bases:
                inserted_bases.append(module.DllBase)
                windows_insert_module(0, 0, module, update_symbols)

    for proc in procs:
        p_pid = proc["pid"]
        p_pgd = proc["pgd"]
        # p_name = proc["name"]
        p_kernel_addr = proc["kaddr"]
        if p_pgd == pgd:
            task = obj.Object("_EPROCESS", offset=p_kernel_addr, vm=addr_space)
            # Note: we do not erase the modules we have information for from the list,
            # unless we have a different module loaded at the same base address.
            # In this way, if at some point the module gets unmapped from the PEB list
            # but it is still in memory, we do not loose the information.
            if (p_pid, p_pgd) not in modules:
                modules[(p_pid, p_pgd)] = {}
            for module in task.get_init_modules():
                if module.DllBase not in inserted_bases:
                    inserted_bases.append(module.DllBase)
                    windows_insert_module(p_pid, p_pgd, module, update_symbols)
            for module in task.get_mem_modules():
                if module.DllBase not in inserted_bases:
                    inserted_bases.append(module.DllBase)
                    windows_insert_module(p_pid, p_pgd, module, update_symbols)
            for module in task.get_load_modules():
                if module.DllBase not in inserted_bases:
                    inserted_bases.append(module.DllBase)
                    windows_insert_module(p_pid, p_pgd, module, update_symbols)
            return


def windows_kdbgscan_fast(dtb):
    global last_kdbg
    from utils import ConfigurationManager as conf_m

    try:
        config = conf_m.vol_conf
        config.DTB = dtb
        try:
            addr_space = utils.load_as(config)
        except BaseException:
            # Return silently
            conf_m.addr_space = None
            return 0L
        conf_m.addr_space = addr_space

        if obj.VolMagic(addr_space).KPCR.value:
            kpcr = obj.Object("_KPCR", offset=obj.VolMagic(
                addr_space).KPCR.value, vm=addr_space)
            kdbg = kpcr.get_kdbg()
            if kdbg.is_valid():
                last_kdbg = kdbg.obj_offset
                return long(last_kdbg)

        kdbg = obj.VolMagic(addr_space).KDBG.v()

        if kdbg.is_valid():
            last_kdbg = kdbg.obj_offset
            return long(last_kdbg)

        # skip the KPCR backup method for x64
        memmode = addr_space.profile.metadata.get('memory_model', '32bit')

        version = (addr_space.profile.metadata.get('major', 0),
                   addr_space.profile.metadata.get('minor', 0))

        if memmode == '32bit' or version <= (6, 1):

            # Fall back to finding it via the KPCR. We cannot
            # accept the first/best suggestion, because only
            # the KPCR for the first CPU allows us to find KDBG.
            for kpcr_off in obj.VolMagic(addr_space).KPCR.get_suggestions():

                kpcr = obj.Object("_KPCR", offset=kpcr_off, vm=addr_space)

                kdbg = kpcr.get_kdbg()

                if kdbg.is_valid():
                    last_kdbg = kdbg.obj_offset
                    return long(last_kdbg)
        return 0L
    except BaseException:
        traceback.print_exc()
