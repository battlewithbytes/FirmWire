## Copyright (c) 2022, Team FirmWire
## SPDX-License-Identifier: BSD-3-Clause
import struct
import logging
import subprocess
import os
import tempfile
import shutil
import json

from enum import Enum, auto
from . import PassthroughPeripheral
from .FSD import MTKFSD
from .ap_properties import APSystemProperties
from .ccci_metadata import packet_metadata, ipc_metadata
from .ccci_ipc import IPCDispatcher
from .ccci_ports import ClosedAPPorts
from .ccci_mailbox import MailboxDispatcher

# Linux: SMEM_USER_CCISM_MCU
first_ringbuf_size = 721 * 1024
# Linux: SMEM_USER_CCISM_MCU_EXP
second_ringbuf_size = 121 * 1024


class FSDMode(Enum):
    EMULATED = auto()
    NATIVE = auto()
    COMPARISON = auto()


class SHM_CCIF_Periph(PassthroughPeripheral):
    def enable_control_observer(self):
        from .ccci_ring_observer import RingObserver
        if getattr(self, "ring_observer", None) is None:
            rings = [(offset, "normal-%d" % index) for index, offset in enumerate(self.offsets)]
            rings += [(offset, "exception-%d" % index) for index, offset in enumerate(self.exp_offsets)]
            self.ring_observer = RingObserver(self, rings)

    def control_observation(self):
        return self.ring_observer.facts()

    def hw_read(self, offset, size):
        value = super().hw_read(offset, size)
        observer = getattr(self, "ring_observer", None)
        if observer:
            observer.guest_read(offset, size)
        return value

    def hw_write(self, offset, size, value):
        observer = getattr(self, "ring_observer", None)
        before = observer.before_guest_write(offset, size) if observer else None
        result = super().hw_write(offset, size, value)
        if observer:
            observer.after_guest_write(before, offset, size)
        return result

    def __init__(self, name, address, size, fsd_mode=FSDMode.EMULATED, **kwargs):
        super().__init__(name, address, size, **kwargs)

        self.initialize_fs_data()

        self.fsd_ctx = {"mode": fsd_mode}

        if fsd_mode == FSDMode.EMULATED:
            self.fsd_ctx["emu"] = MTKFSD(
                self.tempdir.name, log=self.log.getChild("FSD")
            )
        elif fsd_mode == FSDMode.NATIVE:
            self.start_ccci_fsd()
        elif fsd_mode == FSDMode.COMPARISON:
            self.fsd_ctx["emu"] = MTKFSD(
                self.tempdir.name, log=self.log.getChild("FSD")
            )
            self.start_ccci_fsd()
        else:
            raise NotImplementedError("Unhandled FSD mode %s" % fsd_mode)

        # md_ccif_ring_buf_init
        n_queues = kwargs["queue_num"]

        # Linux: drivers/misc/mediatek/eccci/hif/ccci_hif_ccif.c
        # (these must match exactly what the MD expects, apparently)
        rx_sizes = kwargs["queue_layout"]["rx_sizes"]
        tx_sizes = kwargs["queue_layout"]["tx_sizes"]
        rx_sizes_exp = kwargs["queue_layout"]["rx_sizes_exp"]
        tx_sizes_exp = kwargs["queue_layout"]["rx_sizes_exp"]

        self.ringbuf_offset = 0
        self.offsets = []
        self.exp_offsets = []
        for n in range(n_queues):
            self.offsets.append(self.ringbuf_offset + 8)
            self.make_ringbuf(rx_sizes[n], tx_sizes[n])
        self.ringbuf_offset = first_ringbuf_size
        for n in range(n_queues):
            self.exp_offsets.append(self.ringbuf_offset + 8)
            self.make_ringbuf(rx_sizes_exp[n], tx_sizes_exp[n])

    def initialize_fs_data(self):
        src_nv = self.machine.loader.loader_args["nv_data"]
        self.log.info("Copying NV data '%s' to temporary storage...", src_nv)
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="ccci_nvdata", dir=self.machine.workspace.base_path()
        )
        shutil.copytree(src_nv, self.tempdir.name + "/mnt")

    def start_ccci_fsd(self):
        # https://stackoverflow.com/questions/46195881/subprocess-dies-when-thread-dies
        import ctypes
        import signal

        prctl = ctypes.CDLL("libc.so.6").prctl

        def set_pdeathsig():
            PR_SET_PDEATHSIG = 1
            prctl(PR_SET_PDEATHSIG, signal.SIGTERM)

        self.log.info("Starting CCCI filesystem daemon...")

        os.mkfifo(self.tempdir.name + "/fsd_fifo_in")
        os.mkfifo(self.tempdir.name + "/fsd_fifo_out")

        debug = self.log_level == logging.DEBUG
        if debug:
            stdout = None
            stderr = None
        else:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL

        proc = subprocess.Popen(
            [os.getcwd() + "/ccci_fsd_linux/ccci_fsd"],
            cwd=self.tempdir.name,
            preexec_fn=set_pdeathsig,
            stdout=stdout,
            stderr=stderr,
        )

        self.fsd_ctx["native_process"] = proc

        # TODO: make sure peripheral snapshot still work with this approach
        self.fsd_ctx["native_pipe_in"] = open(
            self.tempdir.name + "/fsd_fifo_in", "w+b", buffering=0
        )
        self.fsd_ctx["native_pipe_out"] = open(
            self.tempdir.name + "/fsd_fifo_out", "w+b", buffering=0
        )

    def make_ringbuf(self, rx_length, tx_length):
        CCCI_RBF_HEADER = 0xEE0000EE
        CCCI_RBF_FOOTER = 0xFF0000FF
        RINGBUF_CTL_LEN = 8 + 24 + 8  # header, ccci_ringbuf, footer
        rx_length = rx_length * 1024
        tx_length = tx_length * 1024
        self.write_raw(self.ringbuf_offset + 0x0, 4, CCCI_RBF_HEADER)
        self.write_raw(self.ringbuf_offset + 0x4, 4, CCCI_RBF_HEADER)
        self.write_raw(self.ringbuf_offset + 0x8 + 0x8, 4, rx_length)
        self.write_raw(self.ringbuf_offset + 0x8 + 0x14, 4, tx_length)
        self.ringbuf_offset = (
            self.ringbuf_offset + rx_length + tx_length + RINGBUF_CTL_LEN
        )
        self.write_raw(self.ringbuf_offset - 0x4, 4, CCCI_RBF_FOOTER)
        self.write_raw(self.ringbuf_offset - 0x8, 4, CCCI_RBF_FOOTER)


# TAKEN FROM LINUX
"""
struct ccci_ringbuf {
        struct {
                unsigned int read;
                unsigned int write;
                unsigned int length;
        } rx_control, tx_control;
        unsigned char buffer[0];
};
#define CCCI_RINGBUF_CTL_LEN (8+sizeof(struct ccci_ringbuf)+8)
"""

CCIF_PKG_HEADER = 0xAABBAABB
CCIF_PKG_FOOTER = 0xCCDDEEFF


class Ringbuf:
    # offset points to ccci_ringbuf
    def __init__(self, parent, offset, on_reply=None):
        if on_reply is not None and not callable(on_reply):
            raise ValueError("reply notification must be callable")
        self.parent = parent
        self.offset = offset
        self.on_reply = on_reply

    def rx_state(self):
        read = self.parent.read_raw(self.offset + 0, 4)
        write = self.parent.read_raw(self.offset + 4, 4)
        length = self.parent.read_raw(self.offset + 8, 4)
        if (length < 16 or length % 8 or self.offset + 24 + length > len(self.parent.mem)
                or not 0 <= read < length or not 0 <= write < length or read % 8 or write % 8):
            raise ValueError("Invalid CCIF receive-ring bounds or alignment")
        return read, write, length, (write - read) % length

    def readPacket(self):
        read, write, length, available = self.rx_state()
        if not available:
            return None
        if available < 16:
            raise ValueError("Truncated CCIF receive-ring frame")
        base = self.offset + 24

        def span(cursor, size):
            cursor %= length
            first = min(size, length - cursor)
            return bytes(self.parent.mem[base + cursor:base + cursor + first]) + bytes(
                self.parent.mem[base:base + size - first])

        magic, packetlen = struct.unpack("<II", span(read, 8))
        padded = (packetlen + 7) & ~7
        occupied = padded + 16
        if magic != CCIF_PKG_HEADER or occupied > available:
            raise ValueError("Invalid CCIF receive-ring header or packet length")
        if struct.unpack("<II", span(read + 8 + padded, 8)) != (CCIF_PKG_FOOTER, CCIF_PKG_FOOTER):
            raise ValueError("Invalid CCIF receive-ring footer")
        packet = span(read + 8, packetlen)
        self.parent.write_raw(self.offset, 4, (read + occupied) % length)
        return packet

    def writePacket(self, packet):
        # tx
        read = self.parent.read_raw(self.offset + 12 + 0, 4)
        write = self.parent.read_raw(self.offset + 12 + 4, 4)
        length = self.parent.read_raw(self.offset + 12 + 8, 4)
        size = write - read
        if size < 0:
            size = size + length
        availSize = length - size

        # enough space available?
        if availSize < len(packet) + 16:
            assert False
            return False

        rx_length = self.parent.read_raw(self.offset + 8, 4)
        offset = self.offset + 24 + rx_length
        self.parent.write_raw(offset + write, 4, CCIF_PKG_HEADER)
        write = (write + 4) % length
        self.parent.write_raw(offset + write, 4, len(packet))
        write = (write + 4) % length

        firstpart = len(packet)
        secondpart = 0
        if write + firstpart > length:
            secondpart = write + firstpart - length
            firstpart = length - write
        self.parent.mem[offset + write : offset + write + firstpart] = packet[
            :firstpart
        ]
        self.parent.mem[offset : offset + secondpart] = packet[firstpart:]
        write = (write + len(packet)) % length
        if secondpart:
            assert write == secondpart

        if len(packet) % 8:
            # align
            write = (write + 8 - (len(packet) % 8)) % length

        self.parent.write_raw(offset + write, 4, CCIF_PKG_FOOTER)
        write = (write + 4) % length
        self.parent.write_raw(offset + write, 4, CCIF_PKG_FOOTER)
        write = (write + 4) % length

        # advance write pointer
        self.parent.write_raw(self.offset + 12 + 4, 4, write)
        observer = getattr(self.parent, "ring_observer", None)
        if observer is not None:
            observer.queued(self.offset)
        if self.on_reply is not None:
            self.on_reply()
        # print("ringbuf write, write offset now %x" % write)

        return True


# TAKEN FROM LINUX
# ccci_header size 0x10
# md_query_ap_feature size 0xe0
"""
struct ccif_sram_layout {
        struct ccci_header dl_header;
        struct md_query_ap_feature md_rt_data;
        struct ccci_header up_header;
        struct ap_query_md_feature ap_rt_data;
};
struct ccci_header {
        /* do NOT assume data[1] is data length in Rx */
        u32 data[2];
        u16 channel:16;
        u16 seq_num:15;
        u16 assert_bit:1;
        u32 reserved;
} __packed;

struct ccci_feature_support {
        u8 support_mask:4;
        u8 version:4;
};

#define FEATURE_COUNT 64
#define MD_FEATURE_QUERY_PATTERN 0x49434343
#define AP_FEATURE_QUERY_PATTERN 0x43434349
#define CCCI_AP_RUNTIME_RESERVED_SIZE 120
#define CCCI_MD_RUNTIME_RESERVED_SIZE 152

struct md_query_ap_feature {
        u32 head_pattern;
        struct ccci_feature_support feature_set[FEATURE_COUNT];
        u32 tail_pattern;
#if (MD_GENERATION >= 6293)
        u8  reserved[CCCI_MD_RUNTIME_RESERVED_SIZE];
#endif
};

struct ap_query_md_feature {
        u32 head_pattern;
        struct ccci_feature_support feature_set[FEATURE_COUNT];
        u32 share_memory_support;
        u32 ap_runtime_data_addr;
        u32 ap_runtime_data_size;
        u32 md_runtime_data_addr;
        u32 md_runtime_data_size;
# if 2.0
        u32 set_md_mpu_start_addr;
        u32 set_md_mpu_total_size;
# else 2.1
        u32 noncached_mpu_start_addr;
        u32 noncached_mpu_total_size;
        u32 cached_mpu_start_addr;
        u32 cached_mpu_total_size;
        u32 reserve_addr[12];
#end
        u32 tail_pattern;
#if (MD_GENERATION >= 6293)
        u8  reserved[CCCI_AP_RUNTIME_RESERVED_SIZE];
#endif
};
"""

# Taken from Linux kernel and messages
boot_status_info = {
    0x0000: "POLLING_BUS_READY",
    0x0001: "PREINIT_PDAMON",
    0x0002: "PREINIT_BUSMON",
    0x0003: "PREINIT_FRC",
    0x0004: "START_INIT",
    0x0005: "START_P1",
    0x0006: "RESTART_WDT",
    0x0007: "SAVE_RASP",
    0x0008: "SET_C0_COFIG5_K",
    0x0009: "CLR_C0_STATUS_BEV_ERL",
    0x000A: "INTERRUPT_PREINIT",
    0x000B: "CM_L2_INIT",
    0x000C: "CM_INIT",
    0x000D: "PLL_INIT",
    0x000E: "L1_CACHE_INIT",
    0x000F: "L2_CACHE_INIT",
    0x0010: "SET_CM_WT",
    0x0011: "INIT_OTHER_CORES",
    0x0012: "SET_BOOTSLAVE",
    0x0013: "JOIN_CH_DOMAIN",
    0x0014: "ABN_RST_CHECK",
    0x0015: "MPU_INIT",
    0x0016: "START_P2",
    0x0017: "REGION_INIT",
    0x0018: "REGION_INIT_DONE",
    0x0019: "INIT_GPR",
    0x001A: "INIT_EX_STACK",
    0x001B: "SET_C0_EBASE",
    0x001C: "DISPATCH_SP",
    0x001D: "STACK_INIT",
    0x001E: "INIT_VPE1",
    0x0101: "CLIB_BASE_INIT",
    0x0102: "DUMMY_REF",
    0x0103: "WDT_VAR_INIT",
    0x0104: "CCCI_HW_INIT",
    0x0105: "CCCI_HS1",
    0x0106: "EMM_INIT",
    0x0200: "TRACK_HWINIT_START",
    0x0201: "TRACK_HWINIT_END",
    0x0202: "TRACK_HS1_START",
    0x0203: "TRACK_HS1_START_TX",
    0x0204: "TRACK_HS1_TX_TMOUT",
    0x0205: "TRACK_HS1_START_RX",
    0x0206: "TRACK_HS1_RX_TMOUT",
    0x0207: "TRACK_HS1_TX_END",
    0x0208: "TRACK_HS1_END",
    0x0209: "TRACK_HWINIT2_START",
    0x020A: "TRACK_HWINIT2_END",
    0x020B: "TRACK_HS2_START",
    0x020C: "TRACK_HS2_START_TX",
    0x020D: "TRACK_HS2_TX_TMOUT",
    0x020E: "TRACK_HS2_TX_END",
    0x020F: "TRACK_HS2_END",
}

# TAKEN FROM LINUX
"""
#define GPIO_MAX_COUNT_V2 10
#define GPIO_PIN_NAME_STR_MAX_LEN 34
#define ADC_CH_NAME_STR_MAX_LEN 33
struct ccci_rpc_gpio_adc_intput_v2 { /* 10 pin GPIO support */
        u16 reqMask;
        u16 gpioValidPinMask;
        char gpioPinName[GPIO_MAX_COUNT_V2][GPIO_PIN_NAME_STR_MAX_LEN];
        u32 gpioPinNum[GPIO_MAX_COUNT_V2];
        char adcChName[ADC_CH_NAME_STR_MAX_LEN];
        u32 adcChNum;
        u32 adcChMeasCount;
} __packed;

struct ccci_rpc_gpio_adc_output_v2 { /* 10 pin GPIO support */
        u32 gpioPinNum[GPIO_MAX_COUNT_V2];
        u32 gpioPinValue[GPIO_MAX_COUNT_V2];
        u32 adcChNum;
        u32 adcChMeasSum;
} __packed;
"""

# Linux: drivers/misc/mediatek/eccci/port/port_rpc.h
IPC_RPC_CPSVC_SECURE_ALGO_OP = 0x2001
IPC_RPC_GET_SECRO_OP = 0x2002
IPC_RPC_GET_TDD_EINT_NUM_OP = 0x4001
IPC_RPC_GET_GPIO_NUM_OP = 0x4002
IPC_RPC_GET_ADC_NUM_OP = 0x4003
IPC_RPC_GET_EMI_CLK_TYPE_OP = 0x4004
IPC_RPC_GET_EINT_ATTR_OP = 0x4005
IPC_RPC_GET_GPIO_VAL_OP = 0x4006
IPC_RPC_GET_ADC_VAL_OP = 0x4007
IPC_RPC_GET_RF_CLK_BUF_OP = 0x4008
IPC_RPC_GET_GPIO_ADC_OP = 0x4009
IPC_RPC_USIM2NFC_OP = 0x400A
IPC_RPC_DSP_EMI_MPU_SETTING = 0x400B
IPC_RPC_CCCI_LHIF_MAPPING = 0x400D
IPC_RPC_DTSI_QUERY_OP = 0x400E
IPC_RPC_QUERY_AP_SYS_PROPERTY = 0x400F
IPC_RPC_IT_OP = 0x4321
RPC_API_RESP_ID = 0xFFFF0000

CCCI_RPC_TX = 33


class PCCIF_Periph(PassthroughPeripheral):
    def __init__(self, name, address, size, pccifid, ringbuffer, ap_properties=None, ipc_dispatcher=None, closed_ports=None, mailbox_dispatcher=None, reply_notifications="disabled", **kwargs):
        if reply_notifications not in ("disabled", "ring-index-channel-bit-analysis"):
            raise ValueError("Unsupported CCIF reply notification ABI")
        if reply_notifications != "disabled" and pccifid != 0:
            raise ValueError("Reply notifications require the selected ring transport")
        super().__init__(name, address, size, **kwargs)

        self.pccifid = pccifid
        self.pccif_version = kwargs.get("version", 1)
        self.rchnum = 0
        self.ringbuffer = ringbuffer.resolve()
        self.reply_doorbell = None
        if reply_notifications != "disabled":
            from firmwire.hw.channel_doorbell import ChannelDoorbell
            self.reply_doorbell = ChannelDoorbell(len(self.ringbuffer.offsets))
        if ap_properties is not None and not isinstance(ap_properties, APSystemProperties):
            raise ValueError("ap_properties must be an APSystemProperties instance")
        self.ap_properties = ap_properties if ap_properties is not None else APSystemProperties()
        if ipc_dispatcher is not None and not isinstance(ipc_dispatcher, IPCDispatcher):
            raise ValueError("ipc_dispatcher must be an IPCDispatcher instance")
        self.ipc_dispatcher = ipc_dispatcher
        if closed_ports is not None and not isinstance(closed_ports, ClosedAPPorts):
            raise ValueError("closed_ports must be a ClosedAPPorts instance")
        self.closed_ports = closed_ports
        if mailbox_dispatcher is not None:
            if not isinstance(mailbox_dispatcher, MailboxDispatcher):
                raise ValueError("mailbox_dispatcher must be a MailboxDispatcher instance")
            mailbox_dispatcher.validate_channels({0, 0xe, 0x20, 0x22})
            mailbox_dispatcher.validate_channels(closed_ports.channels if closed_ports else ())
        self.mailbox_dispatcher = mailbox_dispatcher

    def enable_control_observer(self):
        from firmwire.hw.register_observer import RegisterAccessObserver
        from .ccci_boot_observer import CCCIBootObserver
        if not hasattr(self, "register_observer"):
            # Control registers only: do not retain SRAM message payloads.
            self.register_observer = RegisterAccessObserver(0x100)
            if self.pccifid == 0:
                self.boot_observer = CCCIBootObserver()

    def control_observation(self):
        facts = self.register_observer.snapshot()
        doorbell = getattr(self, "reply_doorbell", None)
        if doorbell is not None:
            facts["reply_notifications"] = dict(doorbell.snapshot(), analysis_only=True,
                cpu_interrupt_connected=hasattr(self, "reply_irq_router"), application_delivery_verified=False)
        if hasattr(self, "reply_irq_router"):
            facts["reply_irq_router"] = self.reply_irq_router.snapshot()
        if hasattr(self, "boot_observer"):
            facts["boot_protocol"] = self.boot_observer.snapshot()
        return facts

    # 0 CON, 4 BUSY, C TCHNUM, 14 ACK, 100 CHDATA
    def hw_read(self, offset, size):
        value = self._read_register(offset, size)
        if hasattr(self, "register_observer") and 0 <= offset < 0x100:
            self.register_observer.record("read", offset, size, value)
        return value

    def _read_register(self, offset, size):
        if offset == 0x0:
            # CON
            # (only used by pccif1 to OR on a bit?)
            return super().hw_read(offset, size)
        elif offset == 0x8:
            # START
            return 0
        elif offset == 0x10:
            # RCHNUM
            # one bit per channel
            doorbell = getattr(self, "reply_doorbell", None)
            pending = self.rchnum | (doorbell.pending if doorbell is not None else 0)
            self.log.debug(f"RCHNUM {pending:x}")
            return pending
        elif offset >= 0x100 and offset < 0x100 + 0x200:
            # this is SRAM
            return super().hw_read(offset, size)
        else:
            self.log.error(f"PCCIF {offset:x}")
            assert False

    def hw_write(self, offset, size, value):
        result = self._write_register(offset, size, value)
        if hasattr(self, "register_observer") and 0 <= offset < 0x100:
            self.register_observer.record("write", offset, size, value)
        return result

    def _write_register(self, offset, size, value):
        if offset == 0x0:
            # CON
            # (only used by pccif1 to OR on a bit?)
            return super().hw_write(offset, size, value)
        elif offset == 0xC:
            # TCHNUM
            self.log.debug(f"PCCIF write TCHNUM {value:x}")
            if self.pccifid == 1:
                assert value == 0
                self.handleAMMS()
                return True
            if value == 15:  # TODO: should prbly be >=15
                # RINGQ_SRAM
                self.handle_SRAM_write()
            else:
                if value >= len(self.ringbuffer.offsets):
                    self.log.error(
                        f"PCCIF ring no too large (value: {value}, is only: {len(self.ringbuffer.offsets)})"
                    )
                    assert False
                doorbell = getattr(self, "reply_doorbell", None)
                notify = (lambda: doorbell.notify(value)) if doorbell is not None else None
                ring = Ringbuf(self.ringbuffer, self.ringbuffer.offsets[value], on_reply=notify)
                capacity = self.ringbuffer.read_raw(ring.offset + 8, 4)
                # A doorbell can cover several queued packets. Bound the work;
                # never silently leave the rest behind or loop without limit.
                for _ in range(capacity // 16 + 1):
                    ring_metadata = {"ring_index": value, "ring_offset": ring.offset,
                                     "read": self.ringbuffer.read_raw(ring.offset, 4),
                                     "write": self.ringbuffer.read_raw(ring.offset + 4, 4),
                                     "capacity": capacity}
                    packet = ring.readPacket()
                    if packet is None:
                        break
                    self.dispatch_ccci_packet(ring, bytes(packet), ring_metadata)
                else:
                    raise RuntimeError("CCIF receive ring exceeded bounded drain budget")
        elif offset == 0x14:
            # ACK
            doorbell = getattr(self, "reply_doorbell", None)
            pending = self.rchnum | (doorbell.pending if doorbell is not None else 0)
            self.log.debug(f"ACK {pending:x}")
            if (pending & value) != value:
                self.log.warning("ACKed channels which aren't ready!")
            self.rchnum &= ~value
            if doorbell is not None:
                doorbell.acknowledge(value)
        elif offset == 0x20 or offset == 0x24:
            # IRQ0/1 mask, pccif1 only?
            return super().hw_write(offset, size, value)
        elif offset >= 0x100 and offset < 0x100 + 0x200:
            # this is SRAM
            self.log.debug(f"PCCIF write SRAM {offset:x} {value:x}")
            if self.pccifid == 0 and offset == 0x2E0:
                if value in boot_status_info:
                    self.log.info("boot status: %s", boot_status_info[value])
            return super().hw_write(offset, size, value)
        else:
            self.log.error(f"PCCIF write {offset:x} {value:x}")
            assert False
        return True

    def dispatch_ccci_packet(self, ring, packet, ring_metadata):
        metadata = dict(packet_metadata(packet), **ring_metadata)
        if not metadata["header_complete"]:
            self.log.error("Truncated CCCI header metadata=%s", json.dumps(metadata, sort_keys=True))
            raise ValueError("Truncated CCCI header")
        channel = metadata["channel"]
        if channel == 0x20:
            self.handleRPCPacket(ring, packet)
        elif channel == 0xe:
            self.handleFSPacket(ring, packet)
        elif channel == 0:
            self.handleControlPacket(ring, packet)
        elif channel == 0x22 and getattr(self, "ipc_dispatcher", None) is not None:
            metadata["ipc"] = ipc_metadata(packet)
            try:
                disposition = self.ipc_dispatcher.receive(packet)
            except (ValueError, NotImplementedError):
                self.log.error("Rejected IPC metadata=%s", json.dumps(metadata, sort_keys=True))
                raise
            self.log.info("IPC disposition metadata=%s", json.dumps(
                dict(metadata, disposition=disposition), sort_keys=True))
        elif getattr(self, "mailbox_dispatcher", None) is not None and self.mailbox_dispatcher.accepts(channel):
            try:
                result = self.mailbox_dispatcher.receive(packet)
                queued = False
                if result.response is not None:
                    if not ring.writePacket(result.response.encode()):
                        raise RuntimeError("CCCI mailbox response queue failed")
                    queued = True
            except (ValueError, TypeError, NotImplementedError, RuntimeError):
                self.log.error("Rejected mailbox metadata=%s", json.dumps(metadata, sort_keys=True))
                raise
            # Queueing is not guest receipt, application success, or an AP peer.
            self.log.info("Mailbox disposition metadata=%s", json.dumps(dict(
                metadata, disposition=result.disposition, response_queued=queued,
                peer_connected=False, application_verified=False), sort_keys=True))
        elif getattr(self, "closed_ports", None) is not None and self.closed_ports.accepts(channel):
            disposition = self.closed_ports.receive(packet)
            self.log.info("Closed port disposition metadata=%s", json.dumps(
                dict(metadata, disposition=disposition), sort_keys=True))
        else:
            if channel == 0x22:
                metadata["ipc"] = ipc_metadata(packet)
            self.log.error("Unknown channel metadata=%s", json.dumps(metadata, sort_keys=True))
            raise NotImplementedError("Unknown CCCI channel")

    def handleAMMS(self):
        AMMS_CMD_INIT = 1
        AMMS_CMD_QUERY_DRDI_STATUS = 2
        AMMS_CMD_MPU = 3
        AMMS_CMD_STATIC_FREE = 4
        AMMS_CMD_DYNAMIC_FREE = 5
        AMMS_CMD_DYNAMIC_RETRIEVE = 6
        AMMS_CMD_DEALLOCATE_POS_BUFFER = 7
        AMMS_CMD_ALLOCATE_POS_BUFFER = 8
        AMMS_CMD_QUERY_POS_STATUS = 9

        # request
        cmd, seq_id = struct.unpack("<BB", bytes(self.mem[0x100 + 0 : 0x100 + 2]))
        self.log.debug("AMMS cmd " + str(cmd) + ", seq id " + str(seq_id))

        status = 0
        err = 0
        xtra = bytes()
        if cmd == AMMS_CMD_INIT:
            debug = 1
            num_mpus = 2
            addr = 0
            length = 0
            xtra = struct.pack(
                "<BBBBII", self.pccif_version, debug, num_mpus, 0, addr, length
            )
        elif cmd == AMMS_CMD_QUERY_DRDI_STATUS:
            pass  # do nothing, which is (error?) path where AP already owns dynamic region
        elif cmd == AMMS_CMD_MPU:
            pass
        elif cmd == AMMS_CMD_STATIC_FREE:
            pass
        else:
            self.log.error("Unhandled AMS cmd %x", cmd)
            assert False

        # response
        self.mem[0x100 + 0 : 0x100 + 4] = struct.pack("<BBBB", status, err, seq_id, 0)
        self.mem[0x100 + 4 : 0x100 + 4 + len(xtra)] = xtra
        self.rchnum = self.rchnum | (1 << 0)

    def _handle_fs_packet_native(self, ring, buff):
        ring.parent.fsd_ctx["native_pipe_in"].write(struct.pack("<I", len(buff)))
        ring.parent.fsd_ctx["native_pipe_in"].write(buff)

        self.log.debug("FS: wrote")

        last_packet = None

        while True:
            self.log.debug("FS: try read")
            inbuf = ring.parent.fsd_ctx["native_pipe_out"].read(4)
            insize = struct.unpack("<I", inbuf)[0]
            if insize == 0:
                break
            if insize == 0xFFFFFFFF:
                break

            self.log.debug("FS: trying read %x bytes" % insize)
            inbuf = bytes()

            while len(inbuf) != insize:
                inbuf = inbuf + ring.parent.fsd_ctx["native_pipe_out"].read(
                    insize - len(inbuf)
                )

            last_packet = inbuf
            ring.writePacket(inbuf)

        self.log.debug("FS: done")

        return last_packet

    def _handleFSPacketNative(self, ring, buff):
        self._handle_fs_packet_native(ring, buff)

    def _handleFSPacketEmu(self, ring, buff):
        emu_resp = ring.parent.fsd_ctx["emu"].handle_packet(
            struct.pack("<I", len(buff)) + buff
        )

        # if no data was returned, don't write anything
        if len(emu_resp):
            ring.writePacket(emu_resp)

    def _handleFSPacketCompare(self, ring, buff):
        # only this will write to the guest
        inbuf = self._handle_fs_packet_native(ring, buff)

        # only used for comparison
        resp = ring.parent.fsd_ctx["emu"].handle_packet(
            struct.pack("<I", len(buff)) + buff
        )

        # Fragmented packets
        if inbuf is None:
            if len(resp) == 0:
                self.log.info("CCCI_DBG: REFERENCE FRAG PKT MATCH")
            else:
                self.log.info("CCCI_DBG: REFERENCE FRAG PKT MISMATCH!!!!!!!!!!")

            return

        # truncate due to uninitialized memory from CCCI FSD
        if inbuf[:-2] == resp[:-2]:
            self.log.info("CCCI_DBG: REFERENCE PKT MATCH")
        else:
            self.log.info("CCCI_DBG: REFERENCE PKT MISMATCH!!!!!!!!!!")
            self.log.info("CCCI_DBG: REF %s (%d)", inbuf, len(inbuf))
            self.log.info("CCCI_DBG: EMU %s (%d)", resp, len(resp))

    def handleFSPacket(self, ring, buff):
        # This method will only be called once
        # We lazily replace this method to avoid the mode check
        # As such, mode cannot be dynamically changed
        fsd_mode = ring.parent.fsd_ctx["mode"]

        if fsd_mode == FSDMode.EMULATED:
            self.handleFSPacket = self._handleFSPacketEmu
        elif fsd_mode == FSDMode.NATIVE:
            self.handleFSPacket = self._handleFSPacketNative
        elif fsd_mode == FSDMode.COMPARISON:
            self.handleFSPacket = self._handleFSPacketCompare
        else:
            assert 0

        # finally call the new handler
        return self.handleFSPacket(ring, buff)

    def handleRPCPacket(self, ring, buff):
        # rpc_msg_handler

        if len(buff) < 24:
            raise ValueError("Truncated CCCI RPC header")

        # seq_num also has assert_bit
        data0, data1, channel, seq_num, reserved = struct.unpack("<IIHHI", buff[:16])
        op_id, para_num = struct.unpack("<II", buff[16:24])

        offset = 24
        packets = []
        for n in range(para_num):
            if offset + 4 > len(buff):
                raise ValueError("Truncated CCCI RPC parameter length")
            pktlen = struct.unpack("<I", buff[offset : offset + 4])[0]
            if offset + pktlen + 4 > len(buff):
                raise ValueError("Truncated CCCI RPC parameter")
            packets.append(buff[offset + 4 : offset + 4 + pktlen])
            offset = offset + pktlen + 4
            if pktlen % 4:
                # align
                offset = offset + 4 - (pktlen % 4)

        res_packets = []

        if op_id == IPC_RPC_QUERY_AP_SYS_PROPERTY:
            try:
                name, present, res_packets = self.ap_properties.query(packets)
            except (ValueError, UnicodeError) as exc:
                self.log.warning("Invalid AP property RPC: %s", exc)
                res_packets = [struct.pack("<i", -2)]  # RPC_PARAM_ERROR
            else:
                self.log.info(
                    "AP property RPC name=%r present=%s source=%s value_bytes=%d; "
                    "standalone service, not live AP IPC",
                    name, present, self.ap_properties.source, len(res_packets[1]) - 1,
                )
        elif op_id == IPC_RPC_GET_EINT_ATTR_OP:
            self.log.debug(
                "GET_EINT_ATTR "
                + repr(packets[0])
                + ", type "
                + str(struct.unpack("<I", packets[2])[0])
            )
            res_packets.append(struct.pack("<I", 0))  # ret
            res_packets.append(struct.pack("<I", 0))  # sim_id, TODO: does it matter
        elif op_id == IPC_RPC_GET_GPIO_ADC_OP:
            # Linux: ccci_rpc_get_gpio_adc_v2
            res_packets.append(struct.pack("<I", 0))  # ret
            res_packets.append(24 * struct.pack("<I", 0))  # output
        elif op_id == IPC_RPC_DTSI_QUERY_OP:
            req, index = struct.unpack("<BB", packets[0][:2])
            name = packets[0][2 : 2 + 64]
            self.log.info("DTSI query " + repr(name))
            assert req == 1  # RPC_REQ_PROP_VALUE

            val = 0
            res_packets.append(struct.pack("<I", val) + b"\x00" * 64)
        else:
            self.log.error("Unhandled IPC packet %s", buff.hex())
            self.log.error("RPC header: %s", (data0, data1, channel, seq_num, reserved, op_id, para_num))
            self.log.error(packets)
            assert False

        newbuff = bytes()
        for rp in res_packets:
            newbuff = newbuff + struct.pack("<I", len(rp))
            newbuff = newbuff + rp
            newbuff += b"\x00" * (-len(rp) % 4)
        # CCCI stream framing includes its 16-byte header, operation and count.
        data1 = len(newbuff) + 24
        channel = CCCI_RPC_TX
        op_id = op_id | RPC_API_RESP_ID
        para_num = len(res_packets)
        # seq_num = 1 # FIXME: see Linux ccci_md_inc_tx_seq_num, also check: does this even matter?
        newbuff = (
            struct.pack(
                "<IIHHIII", data0, data1, channel, seq_num, reserved, op_id, para_num
            )
            + newbuff
        )
        self.log.debug(buff.hex())
        self.log.debug(newbuff.hex())
        ring.writePacket(newbuff)

    def handleControlPacket(self, ring, buff):
        # Linux ccci_fsm_recv_control_packet, TODO: merge with handle_SRAM_write, below
        if hasattr(self, "boot_observer"):
            self.boot_observer.control(buff)
        data0, data1, channel, seq_num, reserved = struct.unpack("<IIHHI", buff[:16])

        self.log.info(f"control message {data1},{reserved}")
        if data1 == 0:
            # MD_INIT_START_BOOT
            # second time we see this: HS2
            # self.log.info("FSM: MD reports that it finished booting \( ﾟヮﾟ)/")
            self.log.info("FSM: MD boot control message received (readiness not inferred)")
        else:
            self.log.error("Unhandled control packet %x", data1)
            assert False

    def handle_SRAM_write(self):
        # ccci_fsm_recv_control_packet
        if hasattr(self, "boot_observer"):
            self.boot_observer.hs1(bytes(self.read_raw(0x100 + i, 1) for i in range(88)))
        msg = self.mem[0x100 + 4]
        if msg == 0:
            # MD_INIT_START_BOOT
            self.log.info("FSM: starting boot")

            # ccci_md_prepare_runtime_data
            MD_FEATURE_QUERY_PATTERN = 0x49434343  # "CCCI"
            head_pattern = self.read_raw(0x110, 4)
            tail_pattern = self.read_raw(0x154, 4)
            assert (
                head_pattern == MD_FEATURE_QUERY_PATTERN
                and tail_pattern == MD_FEATURE_QUERY_PATTERN
            )
            # TODO: check features?

            # ccci_md_send_runtime_data
            MD_INIT_CHK_ID = 0x5555FFFF
            AP_FEATURE_QUERY_PATTERN = 0x43434349  # "ICCC"
            MULTI_MD_MPU_SUPPORT = 2
            CCCI_SMEM_SIZE_RUNTIME_AP = 0x800
            CCCI_SMEM_SIZE_RUNTIME_MD = 0x800

            #  ccif_hif_fill_rt_header
            self.write_raw(0x100 + 0xF0 + 0x0, 4, 0)
            self.write_raw(
                0x100 + 0xF0 + 0x4, 4, 0xAC
            )  # packet size: Linux: sizeof(struct ap_query_md_feature_v2_1) + sizeof(struct ccci_header)
            self.write_raw(0x100 + 0xF0 + 0xC, 4, MD_INIT_CHK_ID)
            self.write_raw(0x100 + 0xF0 + 0x8, 4, 1)  # tx_ch (CCCI_CONTROL_TX)

            #  config_ap_runtime_data_v2_1
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x0, 4, AP_FEATURE_QUERY_PATTERN
            )  # head_pattern
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x44, 4, MULTI_MD_MPU_SUPPORT
            )  # share_memory_support

            # these is address of the SMEM_USER_RAW_RUNTIME_DATA which has a list of features:
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x48, 4, 0x69000000
            )  # [arbitrary] ap_runtime_data_addr
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x4C, 4, CCCI_SMEM_SIZE_RUNTIME_AP
            )  # ap_runtime_data_size
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x50, 4, 0x69000000 + CCCI_SMEM_SIZE_RUNTIME_AP
            )  # [arbitrary] md_runtime_data_addr
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x54, 4, CCCI_SMEM_SIZE_RUNTIME_MD
            )  # md_runtime_data_size

            # this is the real shm region, mapped at 0x40000000 on real device? but MPU problems if we try, seems we can ignore for now
            # self.write_raw(0x100 + 0xf0 + 0x10 + 0x58, 4, 0x40000000) # [arbitrary] bla
            # self.write_raw(0x100 + 0xf0 + 0x10 + 0x5c, 4, 0x800) # [arbitrary] bla size
            # self.write_raw(0x100 + 0xf0 + 0x10 + 0x60, 4, 0x40000000 + 0x8000000) # [arbitrary] bla
            # self.write_raw(0x100 + 0xf0 + 0x10 + 0x60, 4, 0x40000000 + 0x800) # [arbitrary] bla
            # self.write_raw(0x100 + 0xf0 + 0x10 + 0x64, 4, 0x800) # [arbitrary] bla size

            # 0x98 because we're using v2.1 struct
            self.write_raw(
                0x100 + 0xF0 + 0x10 + 0x98, 4, AP_FEATURE_QUERY_PATTERN
            )  # tail_pattern

            # done, send message back
            if hasattr(self, "boot_observer"):
                self.boot_observer.ap_response(bytes(self.read_raw(0x1F0 + i, 1) for i in range(0xAC)))
            self.rchnum = self.rchnum | (1 << 15)  # TODO: why 15
        else:
            self.log.error("FSM: unknown message %x", msg)
            assert False
