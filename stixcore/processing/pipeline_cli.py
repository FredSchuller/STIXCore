import os
import re
import sys
import shutil
import logging
import argparse
import warnings
from enum import Enum
from pathlib import Path
from collections import defaultdict

from stixcore.config.config import CONFIG
from stixcore.ephemeris.manager import Spice, SpiceKernelManager
from stixcore.io.soc.manager import SOCManager, SOCPacketFile
from stixcore.processing.L0toL1 import Level1
from stixcore.processing.L1toL2 import Level2
from stixcore.processing.LBtoL0 import Level0
from stixcore.processing.pipeline import PipelineStatus
from stixcore.processing.TMTCtoLB import process_tmtc_to_levelbinary
from stixcore.soop.manager import SOOPManager
from stixcore.tmtc.packets import TMTC

warnings.filterwarnings('ignore', module='astropy.io.fits.card')
warnings.filterwarnings('ignore', module='astropy.utils.metadata')


def clear_dir(dir):
    for files in os.listdir(dir):
        path = os.path.join(dir, files)
        try:
            shutil.rmtree(path)
        except OSError:
            os.remove(path)


class ProductLevel(Enum):
    """Enum Type for Processing steps to make them sortable"""
    TM = -2
    LB = -1
    L0 = 0
    L1 = 1
    L2 = 2
    ALL = 100

    def __str__(self):
        return self.name

    @staticmethod
    def from_str(label):
        label = label.upper()
        for e in ProductLevel:
            if e.name == label:
                return e
        return ProductLevel.TM


def main():

    parser = argparse.ArgumentParser(description='stix pipeline processing')

    # pathes
    parser.add_argument("-t", "--tm_dir",
                        help="input directory to the (tm xml) files",
                        default=CONFIG.get('Paths', 'tm_archive'), type=str)

    parser.add_argument("-f", "--fits_dir",
                        help="output directory for the ",
                        default=CONFIG.get('Paths', 'fits_archive'), type=str)

    parser.add_argument("-s", "--spice_dir",
                        help="directory to the spice kernels files",
                        default=CONFIG.get('Paths', 'spice_kernels'), type=str)

    parser.add_argument("-S", "--spice_file",
                        help="path to the spice meta kernel",
                        default=None, type=str)

    parser.add_argument("-p", "--soop_dir",
                        help="directory to the SOOP files",
                        default=CONFIG.get('Paths', 'soop_files'), type=str)

    # IDL Bridge
    parser.add_argument("--idl_enabled",
                        help="IDL is setup to interact with the pipeline",
                        default=CONFIG.getboolean('IDLBridge', 'enabled', fallback=False),
                        action='store_true', dest='idl_enabled')
    parser.add_argument("--idl_disabled",
                        help="IDL is setup to interact with the pipeline",
                        default=not CONFIG.getboolean('IDLBridge', 'enabled', fallback=False),
                        action='store_false', dest='idl_enabled')

    parser.add_argument("--idl_gsw_path",
                        help="directory where the IDL gsw is installed",
                        default=CONFIG.get('IDLBridge', 'gsw_path'), type=str)

    parser.add_argument("--idl_batchsize",
                        help="batch size how many TM prodcts batched by the IDL bridge",
                        default=CONFIG.getint('IDLBridge', 'batchsize', fallback=10), type=int)

    # logging
    parser.add_argument("--stop_on_error",
                        help="the pipeline stops on any error",
                        default=CONFIG.getboolean('Logging', 'stop_on_error', fallback=False),
                        action='store_true', dest='stop_on_error')

    parser.add_argument("--continue_on_error",
                        help="the pipeline reports any error and continouse processing",
                        default=not CONFIG.getboolean('Logging', 'stop_on_error', fallback=False),
                        action='store_false', dest='stop_on_error')

    parser.add_argument("-o", "--out_file",
                        help="file all processed files will be logged into",
                        default=None, type=str)
    parser.add_argument("-l", "--log_file",
                        help="a optional file all logging is appended",
                        default=None, type=str)
    parser.add_argument("--log_level",
                        help="the level of logging",
                        default=None, type=str,
                        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"])

    # processing
    parser.add_argument("-b", "--start_level",
                        help="the processing level where to start",
                        default="TM", type=str, choices=[e.name for e in ProductLevel])
    parser.add_argument("-e", "--end_level",
                        help="the processing level where to stop the pipeline",
                        default="L2", type=str, choices=[e.name for e in ProductLevel])
    parser.add_argument("--filter", "-F",
                        help="filter expression applied to all input files example '*sci*.fits'",
                        default=None, type=str, dest='filter')
    parser.add_argument("--input_files", "-i",
                        help="input txt file with list af absolute paths of files to process",
                        default=None, type=str, dest='input_files')
    parser.add_argument("-c", "--clean",
                        help="clean all files from <fits_dir> first",
                        default=False, type=bool, const=True, nargs="?")

    args = parser.parse_args()

    # pathes
    CONFIG.set('Paths', 'tm_archive', args.tm_dir)
    CONFIG.set('Paths', 'fits_archive', args.fits_dir)
    CONFIG.set('Paths', 'spice_kernels', args.spice_dir)
    CONFIG.set('Paths', 'soop_files', args.soop_dir)

    # IDL Bridge
    CONFIG.set('IDLBridge', 'enabled', str(args.idl_enabled))
    CONFIG.set('IDLBridge', 'gsw_path', args.idl_gsw_path)
    CONFIG.set('IDLBridge', 'batchsize', str(args.idl_batchsize))

    # logging
    CONFIG.set('Logging', 'stop_on_error', str(args.stop_on_error))

    logging.basicConfig(format='%(asctime)s %(message)s', force=True,
                        filename=args.log_file, filemode="a+")

    if args.log_level:
        logging.getLogger().setLevel(logging.getLevelName(args.log_level))

    # processing
    args.start_level = ProductLevel.from_str(args.start_level)
    args.end_level = ProductLevel.from_str(args.end_level)

    if args.end_level.value < args.start_level.value:
        raise ValueError(f"--start_level ({args.start_level}) should be lover then"
                         f"--end_level ({args.end_level})")

    if args.spice_file:
        spicemeta = Path(args.spice_file)
    else:
        _spm = SpiceKernelManager(Path(CONFIG.get('Paths', 'spice_kernels')))
        spicemeta = _spm.get_latest_mk(top_n=30)

    Spice.instance = Spice(spicemeta)
    print(f"Spice kernel @: {Spice.instance.meta_kernel_path}")

    soc = SOCManager(Path(CONFIG.get('Paths', 'tm_archive')))

    SOOPManager.instance = SOOPManager(Path(CONFIG.get('Paths', 'soop_files')))

    fitsdir = Path(CONFIG.get('Paths', 'fits_archive'))
    FILTER = args.filter

    l0_proc = Level0(fitsdir, fitsdir)
    l1_proc = Level1(fitsdir, fitsdir)
    l2_proc = Level2(fitsdir, fitsdir)

    PipelineStatus.log_setup()

    input_files = list()

    processed_files = defaultdict(list)

    if args.input_files:
        try:
            p = re.compile('.*' if not FILTER else FILTER)
            with open(args.input_files, "r") as f:
                for ifile in f:
                    try:
                        ifile = Path(ifile.strip())
                        if len(ifile.name) > 1 and ifile.exists() and p.match(str(ifile)):
                            input_files.append(ifile)
                    except Exception as e:
                        print(e)
                        print("skipping this input file line")
        except IOError as e:
            print(e)
            print("Fall back to default input")

    has_input_files = len(input_files) > 0

    if args.clean and fitsdir.exists:
        confirm = input(f"clean {fitsdir} Y/N: ")
        if confirm.upper() == "Y":
            clear_dir(fitsdir)

    if ProductLevel.LB.value >= args.start_level.value:
        if has_input_files:
            tmfiles = [SOCPacketFile(f) in input_files]
        else:
            tmfiles = soc.get_files(tmtc=TMTC.All if FILTER is None else FILTER)

        processed_files[ProductLevel.LB].extend(
            list(process_tmtc_to_levelbinary(tmfiles, archive_path=fitsdir)))
        FILTER = None

    if args.start_level == ProductLevel.L0:
        if not FILTER:
            FILTER = "*.fits"
        if has_input_files:
            processed_files[ProductLevel.LB].extend(input_files)
        else:
            processed_files[ProductLevel.LB].extend((fitsdir / "LB").rglob(FILTER))

    if ((ProductLevel.L0.value >= args.start_level.value)
            and (args.end_level.value >= ProductLevel.L0.value)):
        processed_files[ProductLevel.L0].extend(
            l0_proc.process_fits_files(files=processed_files[ProductLevel.LB]))
        FILTER = None

    if args.start_level == ProductLevel.L1:
        if not FILTER:
            FILTER = "*.fits"
        if has_input_files:
            processed_files[ProductLevel.L0].extend(input_files)
        else:
            processed_files[ProductLevel.L0].extend((fitsdir / "L0").rglob(FILTER))

    if ((ProductLevel.L1.value >= args.start_level.value)
            and (args.end_level.value >= ProductLevel.L1.value)):
        processed_files[ProductLevel.L1].extend(
            l1_proc.process_fits_files(files=processed_files[ProductLevel.L0]))
        FILTER = None

    if args.start_level == ProductLevel.L2:
        if not FILTER:
            FILTER = "*.fits"
        if has_input_files:
            processed_files[ProductLevel.L1].extend(input_files)
        else:
            processed_files[ProductLevel.L1].extend((fitsdir / "L1").rglob(FILTER))

    if ((ProductLevel.L2.value >= args.start_level.value)
            and (args.end_level.value >= ProductLevel.L2.value)):
        processed_files[ProductLevel.L2].extend(
            l2_proc.process_fits_files(files=processed_files[ProductLevel.L1]))
        FILTER = None

    outstream = sys.stdout if not args.out_file else open(args.out_file, 'w')
    for le in processed_files.keys():
        for f in processed_files[le]:
            print(str(f), file=outstream)
    outstream.close()


if __name__ == '__main__':
    main()
