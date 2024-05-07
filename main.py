from cuatrorpc import RpcClientCLI
from binascii import unhexlify
import platform
import os, sys
from typing import Optional
import pwinput
import time
import random

OS = platform.system()

if OS == "Linux":
    ghost_data_dir = os.path.expanduser("~/.ghost")
elif OS == "Darwin":
    ghost_data_dir = os.path.expanduser("~/Library/Application Support/Ghost")
elif OS == "Windows":
    ghost_data_dir = os.path.expanduser("~/AppData/Roaming/Ghost")


MIN_TX = 0.00001
MAX_FEE = 0.35


class ConsolidateUTXOs:
    def __init__(self, rpc_cli: RpcClientCLI) -> None:
        self.rpc_cli = rpc_cli
        self.wallet: Optional[str] = None
        self.is_encrypted: bool = False
        self.password: Optional[str] = None
        self.mode: Optional[str] = None
        self.spend_addr: Optional[str] = None
        self.stake_addr: Optional[str] = None
        self.anon_balance: Optional[float] = None
        self.get_wallet_from_user()
        if self.is_wallet_locked() or self.is_encrypted:
            self.get_password_from_user()

        self.my_vetlist = self.get_vet_list()
        self.stealth_addr = self.get_stealth_address()
        self.get_mode_from_user()

    def get_mode_from_user(self):
        while True:
            print("What staking method do you use?")
            print("1. Cold Staking")
            print("2. Hot Staking")
            try:
                mode = int(input())
            except ValueError:
                print("\n\nInvalid selection")
                continue

            if mode < 1 or mode > 2:
                print("\n\nInvalid selection")
                continue
            break

        match mode:
            case 1:
                self.mode = "coldstaking"

                bals = self.get_balances()

                anon_balance = bals.get("anon_trusted", 0)

                if anon_balance:
                    while True:
                        print(
                            f"Detected {round(anon_balance, 8)} anon balance\
                                \nWould you like to preserve the anon balance?\
                                \nSelecting 'n' will result in this balance being zapped to coldstaking (y/n) "
                        )
                        consolidate_anon = input()
                        if consolidate_anon.lower() == "y":
                            print("Anon balance will be preserved")
                            self.anon_balance = round(anon_balance, 8)
                            break
                        elif consolidate_anon.lower() == "n":
                            break
                        else:
                            print("Invalid selection")

                if self.my_vetlist:
                    vet_256bit = [
                        addr
                        for addr in self.my_vetlist
                        if self.get_address_info(addr).get("is_256bit")
                    ]

                    if vet_256bit:
                        while True:
                            print(
                                f"Detected {len(vet_256bit)} 256bit addresse{'s' if len(vet_256bit) != 1 else ''} that are AGVR eligible\nWould you like to use one of these addresses? (y/n) "
                            )
                            use_vetlist = input()
                            if use_vetlist.lower() == "y":
                                while True:
                                    print("Select a vetlist address:")
                                    for i, addr in enumerate(vet_256bit):
                                        print(f"{i + 1}. {addr}")
                                    try:
                                        selected_vet = int(input())
                                    except ValueError:
                                        print("\n\nInvalid selection")
                                        continue

                                    if selected_vet < 1 or selected_vet > len(
                                        vet_256bit
                                    ):
                                        print("\n\nInvalid selection")
                                        continue
                                    self.spend_addr = vet_256bit[selected_vet - 1]
                                    break
                                break
                            elif use_vetlist.lower() == "n":
                                break
                            else:
                                print("Invalid selection")
                if self.spend_addr is None:
                    while True:
                        print(
                            "Please enter the cold staking address (starts with a 2): "
                        )
                        cold_staking_address = input()

                        addr_info = self.get_address_info(cold_staking_address)
                        if not addr_info.get("is_valid"):
                            print("Invalid cold staking address")
                            continue
                        if not addr_info.get("is_256bit"):
                            print("Cold staking address must be a 256-bit address")
                            continue
                        self.spend_addr = cold_staking_address
                        break

                self.stake_addr = self.get_cs_addresses_from_wallet()
                if self.stake_addr:
                    while True:
                        print(
                            f"Would you like to use the wallet's stake address: (y/n)?\n{self.stake_addr}\n"
                        )
                        use_wallet_stake = input()
                        if use_wallet_stake.lower() == "y":
                            break
                        elif use_wallet_stake.lower() == "n":
                            self.stake_addr = None
                            break
                        else:
                            print("Invalid selection")

                if self.stake_addr is None:
                    while True:
                        print(
                            "Plese enter the pools stake address (starts with gcs) or the GhostVault extpubkey: "
                        )
                        stake_address = input()
                        addr_info = self.get_address_info(stake_address)

                        if not addr_info.get("is_valid"):
                            print("Invalid address")
                            continue
                        if not addr_info.get("is_ext_pub_key") and not addr_info.get(
                            "is_stake_only"
                        ):
                            print(
                                "Address must be a Coldstake only address or an extended public key"
                            )
                            continue
                        self.stake_addr = stake_address
                        break

                self.consolidate_low_value_cs_utxos()
                self.consolidate_non_cs_utxos()
                self.consolidate_anon_utxos()
                self.zap_anon()

                print("\nConsolidation complete\nHave a great day!")

            case 2:
                self.mode = "hotstaking"

                self.consolidate_low_value_hs_utxos()
                self.consolidate_anon_utxos()

                print("\nConsolidation complete\nHave a great day!")

    def get_cs_addresses_from_wallet(self):
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if locktime := self.is_wallet_locked() is not None:
            if locktime <= 1:
                self.unlock_wallet(str(self.password), 2)
        change_settings = self.rpc_cli.callrpc(
            "walletsettings", ["changeaddress"], wallet=self.wallet
        )

        if change_settings.get("changeaddress") == "default":
            return None

        return change_settings.get("changeaddress").get("coldstakingaddress")

    def get_address_info(self, address: str) -> dict:
        is_valid = False
        if self.wallet is None:
            raise ValueError("Wallet not set")
        try:
            addr_info = self.rpc_cli.callrpc(
                "getaddressinfo", [address], wallet=self.wallet
            )
            is_valid = True
        except RuntimeError:
            addr_info = {}
            is_valid = False

        is_256bit = addr_info.get("is256bit", False)
        is_ext_pub_key = addr_info.get("isextkey", False)
        is_stake_only = addr_info.get("isstakeonly", False)
        is_mine = addr_info.get("ismine", False)

        ret_val = {
            "is_valid": is_valid,
            "is_256bit": is_256bit,
            "is_ext_pub_key": is_ext_pub_key,
            "is_stake_only": is_stake_only,
            "is_mine": is_mine,
        }
        return ret_val

    def get_wallet_from_user(self) -> None:
        wallets = self.list_wallets()
        if len(wallets) == 0:
            print("No wallets found")
            return
        if len(wallets) == 1:
            self.wallet = wallets[0]
            print(
                f"Selected wallet: {self.wallet if self.wallet != '' else 'Default Wallet'}"
            )
            return
        while True:
            print("Select a wallet:")
            for i, wallet in enumerate(wallets):
                print(f"{i + 1}. {wallet if wallet != '' else 'Default Wallet'}")
            try:
                selected_wallet = int(input())
            except ValueError:
                print("\n\nInvalid selection")
                continue

            if selected_wallet < 1 or selected_wallet > len(wallets):
                print("\n\nInvalid selection")
                continue
            break

        self.wallet = wallets[selected_wallet - 1]
        print(
            f"Selected wallet: {self.wallet if self.wallet != '' else 'Default Wallet'}"
        )

    def get_password_from_user(self) -> None:

        valid_password = False

        while not valid_password:
            password = pwinput.pwinput(prompt="Enter wallet passphrase: ")
            if password == "":
                print("Error: The wallet passphrase entered was incorrect.")
                print("Please try again")
                continue
            try:
                self.unlock_wallet(str(password), 1)
                self.password = password
                valid_password = True
            except KeyboardInterrupt:
                print("\n\nExiting")
                sys.exit()
            except ValueError as e:
                if e == "cannot parse integer from empty string":
                    valid_password = True
            except RuntimeError as e:
                if "The wallet passphrase entered was incorrect." in str(e):
                    print("Error: The wallet passphrase entered was incorrect.")
                    print("Please try again")
                    time.sleep(3)
                else:
                    raise e

    def unlock_wallet(self, passphrase: str, timeout: int = 60) -> None:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        try:

            self.rpc_cli.callrpc(
                "walletpassphrase", [passphrase, timeout], wallet=self.wallet
            )
        except ValueError as e:
            if e == "cannot parse integer from empty string":
                raise e
        except RuntimeError as e:
            if "The wallet passphrase entered was incorrect." in str(e):
                raise e

    def get_wallet_info(self) -> dict:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        return self.rpc_cli.callrpc("getwalletinfo", wallet=self.wallet)

    def get_balances(self) -> dict:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        return self.rpc_cli.callrpc("getbalances", wallet=self.wallet)["mine"]

    def consolidate_non_cs_utxos(self) -> None:
        print("Consolidating UTXOs that are not coldstaking")

        if self.wallet is None:
            raise ValueError("Wallet not set")
        if self.is_encrypted and self.password is None:
            self.get_password_from_user()

        utxos = self.list_unspent()
        total_utxos = len(utxos)
        if not total_utxos:
            print("No UTXOs found")
            return

        non_cs_utxos = [
            utxo
            for utxo in utxos
            if utxo["spendable"]
            and not self.isCsOut(utxo["scriptPubKey"])
            and not utxo["address"].startswith("g")
        ]

        total_non_cs_utxos = len(non_cs_utxos)

        if not total_non_cs_utxos:
            print("No UTXOs that are not already coldstaking found")
            return

        print(f"Total UTXOs: {total_utxos}")
        print(f"Total UTXOs not cold staking: {total_non_cs_utxos}")

        txid = self.process_utxos(non_cs_utxos, "ghost", "anon")

        self.wait_for_tx(txid)

    def consolidate_anon_utxos(self) -> None:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if self.is_encrypted and self.password is None:
            self.get_password_from_user()

        while (anon_utxos := self.list_unspent_anon()) and len(anon_utxos) > 10:
            print("Consolidating anon UTXOs")
            txid = self.process_utxos(anon_utxos, "anon", "anon")
            self.wait_for_tx(txid)

    def zap_anon(self) -> str:
        print("Zapping to coldstaking")
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if self.is_encrypted and self.password is None:
            self.get_password_from_user()

        preserve_balance = self.anon_balance if self.anon_balance is not None else 0
        bals = self.get_balances()
        anon_balance = bals.get("anon_trusted", 0)

        available_balance = anon_balance - preserve_balance

        print(f"Available balance: {available_balance}")

        if available_balance <= 0:
            return "No anon balance to zap"

        left_to_zap = available_balance

        tx_outputs = []
        cs_script = self.get_cs_script()

        while left_to_zap > 0:
            if left_to_zap >= 1500:
                tx_outputs.append(
                    {
                        "address": "script",
                        "amount": 1500,
                        "subfee": True,
                        "script": cs_script,
                    }
                )
                left_to_zap -= 1500

                if len(tx_outputs) >= 300:
                    txid = self.rpc_cli.callrpc(
                        "sendtypeto",
                        [
                            "anon",
                            "ghost",
                            tx_outputs,
                            "",
                            "",
                            12,
                            1,
                            False,
                            {"feeRate": 0.00007500},
                        ],
                        wallet=self.wallet,
                    )

                    print(f"Anon balance zapped: {txid}")

                    tx_outputs = []
                    self.wait_for_tx(txid)

            else:
                tx_outputs.append(
                    {
                        "address": "script",
                        "amount": left_to_zap,
                        "subfee": True,
                        "script": cs_script,
                    }
                )
                left_to_zap = 0

        if not tx_outputs:
            return txid

        txid = self.rpc_cli.callrpc(
            "sendtypeto",
            [
                "anon",
                "ghost",
                tx_outputs,
                "",
                "",
                12,
                1,
                False,
                {"feeRate": 0.00007500},
            ],
            wallet=self.wallet,
        )

        print(f"Anon balance zapped: {txid}")

        return txid

    def consolidate_low_value_cs_utxos(self) -> str:
        print("Consolidating low value UTXOs that are coldstaking")
        print(
            "This is to cleanup excessive UTXO from things like AGVR, pool rewards, small zaps etc."
        )
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if self.is_encrypted and self.password is None:
            self.get_password_from_user()

        utxos = self.list_unspent()
        total_utxos = len(utxos)
        if not total_utxos:
            print("No UTXOs found")
            return "No UTXOs found"

        low_value_cs_utxos = [
            utxo
            for utxo in utxos
            if utxo["spendable"]
            and self.isCsOut(utxo["scriptPubKey"])
            and not utxo["address"].startswith("g")
            and utxo["amount"] < 500
        ]

        total_low_value_cs_utxos = len(low_value_cs_utxos)

        if not total_low_value_cs_utxos:
            print("No UTXOs that are not already coldstaking found")
            return "No UTXOs that are not already coldstaking found"

        utxo_groups = {}

        for utxo in low_value_cs_utxos:
            if utxo["address"] not in utxo_groups:
                utxo_groups[utxo["address"]] = []
            utxo_groups[utxo["address"]].append(utxo)

        txid = ""

        for address, utxos in utxo_groups.items():
            print(f"Total UTXOs: {len(utxos)}")
            if len(utxos) >= 1:
                cs_script = utxos[0]["scriptPubKey"]
                res = self.process_utxos_script(utxos, "ghost", "ghost", cs_script)
                if res:
                    txid = res

        return txid

    def consolidate_low_value_hs_utxos(self) -> None:
        print("Consolidating low value UTXOs that are hotstaking")
        print(
            "This is to cleanup excessive UTXO from things like AGVR, small deposits etc."
        )
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if self.is_encrypted and self.password is None:
            self.get_password_from_user()

        utxos = self.list_unspent()
        total_utxos = len(utxos)
        if not total_utxos:
            print("No UTXOs found")
            return

        low_value_hs_utxos = [
            utxo
            for utxo in utxos
            if utxo["spendable"]
            and not self.isCsOut(utxo["scriptPubKey"])
            and not utxo["address"].startswith("g")
            and utxo["amount"] < 500
        ]

        total_low_value_hs_utxos = len(low_value_hs_utxos)

        if not total_low_value_hs_utxos:
            print("No low value UTXOs found")
            return

        utxo_groups = {}

        for utxo in low_value_hs_utxos:
            if utxo["address"] not in utxo_groups:
                utxo_groups[utxo["address"]] = []
            utxo_groups[utxo["address"]].append(utxo)

        for address, utxos in utxo_groups.items():
            print(f"Total UTXOs: {len(utxos)}")
            if len(utxos) > 2:
                cs_script = utxos[0]["scriptPubKey"]
                txid = self.process_utxos_script(utxos, "ghost", "ghost", cs_script)

    def process_utxos_script(
        self, utxos: list, in_type: str, out_type: str, cs_script: str
    ) -> str:
        tx_inputs = []
        total_amount = 0
        txid = ""

        for index, utxo in enumerate(utxos):
            if not utxo["spendable"]:
                continue
            total_amount += utxo["amount"]
            tx_inputs.append(
                {
                    "tx": utxo["txid"],
                    "n": utxo["vout"],
                }
            )

            if total_amount < MIN_TX:
                total_amount = 0
                tx_inputs = []
                continue

            if len(tx_inputs) % 25 == 0 or index == len(utxos) - 1:
                if locktime := self.is_wallet_locked() is not None:
                    if locktime <= 1:
                        self.unlock_wallet(str(self.password), 2)
                tx_fee = self.rpc_cli.callrpc(
                    "sendtypeto",
                    [
                        in_type,
                        out_type,
                        [
                            {
                                "address": "script",
                                "amount": round(total_amount, 8),
                                "subfee": True,
                                "script": cs_script,
                            }
                        ],
                        "",
                        "",
                        12,
                        1,
                        True,
                        {"inputs": tx_inputs, "feeRate": 0.00007500},
                    ],
                    wallet=self.wallet,
                ).get("fee")

                if tx_fee >= MAX_FEE or index == len(utxos) - 1:
                    if locktime := self.is_wallet_locked() is not None:
                        if locktime <= 1:
                            self.unlock_wallet(str(self.password), 2)
                    txid = self.rpc_cli.callrpc(
                        "sendtypeto",
                        [
                            in_type,
                            out_type,
                            [
                                {
                                    "address": "script",
                                    "amount": round(total_amount, 8),
                                    "subfee": True,
                                    "script": cs_script,
                                }
                            ],
                            "",
                            "",
                            12,
                            1,
                            False,
                            {
                                "inputs": tx_inputs,
                                "feeRate": 0.00007500,
                                "show_hex": True,
                            },
                        ],
                        wallet=self.wallet,
                    )
                    print(f"Consolidation transaction sent: {txid}")
                    time.sleep(0.1)
                    total_amount = 0
                    tx_inputs = []
        return txid

    def get_vet_list(self):
        vetlist = self.rpc_cli.callrpc("geteligibleaddresses", wallet=self.wallet)

        cleaned_vetlist = [vet["Address"] for vet in vetlist if vet["Balance"] >= 20000]

        my_vetlist = [
            addr
            for addr in cleaned_vetlist
            if self.get_address_info(addr).get("is_mine")
        ]

        return my_vetlist

    def process_utxos(self, utxos: list, in_type: str, out_type: str) -> str:
        tx_inputs = []
        total_amount = 0

        safe_key = "spendable" if in_type == "ghost" else "safe"

        for index, utxo in enumerate(utxos):
            total_amount += utxo["amount"]
            if not utxo[safe_key]:
                continue
            tx_inputs.append(
                {
                    "tx": utxo["txid"],
                    "n": utxo["vout"],
                }
            )

            if len(tx_inputs) % 25 == 0 or index == len(utxos) - 1:
                if locktime := self.is_wallet_locked() is not None:
                    if locktime <= 1:
                        self.unlock_wallet(str(self.password), 2)
                tx_fee = self.rpc_cli.callrpc(
                    "sendtypeto",
                    [
                        in_type,
                        out_type,
                        [
                            {
                                "address": self.stealth_addr,
                                "amount": round(total_amount, 8),
                                "subfee": True,
                            }
                        ],
                        "",
                        "",
                        12,
                        1,
                        True,
                        {"inputs": tx_inputs, "feeRate": 0.00007500},
                    ],
                    wallet=self.wallet,
                ).get("fee")

                if tx_fee >= MAX_FEE or index == len(utxos) - 1:
                    if locktime := self.is_wallet_locked() is not None:
                        if locktime <= 1:
                            self.unlock_wallet(str(self.password), 2)
                    txid = self.rpc_cli.callrpc(
                        "sendtypeto",
                        [
                            in_type,
                            out_type,
                            [
                                {
                                    "address": self.stealth_addr,
                                    "amount": round(total_amount, 8),
                                    "subfee": True,
                                }
                            ],
                            "",
                            "",
                            12,
                            1,
                            False,
                            {"inputs": tx_inputs, "feeRate": 0.00007500},
                        ],
                        wallet=self.wallet,
                    )
                    print(f"Consolidation transaction sent: {txid}")
                    time.sleep(0.1)
                    total_amount = 0
                    tx_inputs = []
        return txid

    def get_cs_script(self) -> str:
        if self.wallet is None:
            raise ValueError("Wallet not set")

        stake_addr_info = self.get_address_info(self.stake_addr)

        if stake_addr_info.get("is_ext_pub_key"):
            stake_addr = self.derive_range_keys()
        else:
            stake_addr = self.stake_addr

        print(f"Stake address: {stake_addr}")

        if locktime := self.is_wallet_locked() is not None:
            if locktime <= 1:
                self.unlock_wallet(str(self.password), 2)

        cs_script = self.rpc_cli.callrpc(
            "buildscript",
            [
                {
                    "recipe": "ifcoinstake",
                    "addrstake": stake_addr,
                    "addrspend": self.spend_addr,
                }
            ],
            wallet=self.wallet,
        )

        return cs_script["hex"]

    def wait_for_tx(self, txid: str) -> None:
        if self.wallet is None:
            raise ValueError("Wallet not set")

        target_confirmations = 12

        tx_details = self.rpc_cli.callrpc("gettransaction", [txid], wallet=self.wallet)

        confirms = tx_details.get("confirmations", 0)

        while confirms < target_confirmations:
            print(
                f"Waiting for {target_confirmations - confirms} confirmations. Current confirmations: {confirms}"
            )
            time.sleep(10)
            tx_details = self.rpc_cli.callrpc(
                "gettransaction", [txid], wallet=self.wallet
            )
            confirms = tx_details.get("confirmations", 0)

    def derive_range_keys(self) -> str:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if locktime := self.is_wallet_locked() is not None:
            if locktime <= 1:
                self.unlock_wallet(str(self.password), 2)

        key = random.randint(0, 63)

        range_keys = self.rpc_cli.callrpc(
            "deriverangekeys", [key, key, self.stake_addr], wallet=self.wallet
        )

        return range_keys[0]

    def is_wallet_locked(self) -> int:
        is_locked = self.get_wallet_info().get("unlocked_until")

        if is_locked is None:
            self.is_encrypted = False
            return False
        self.is_encrypted = True
        return is_locked

    def get_stealth_address(self):
        if self.wallet is None:
            raise ValueError("Wallet not set")
        if locktime := self.is_wallet_locked() is not None:
            if locktime <= 1:
                self.unlock_wallet(str(self.password), 2)
        current_stealth_addresses = self.list_stealth_addresses()
        stealth_addr = None

        if current_stealth_addresses:
            stealth_addr_lst = current_stealth_addresses[0].get("Stealth Addresses")
            if stealth_addr_lst:
                stealth_addr = stealth_addr_lst[0].get("Address")

        if stealth_addr is None:
            stealth_addr = self.rpc_cli.callrpc(
                "getnewstealthaddress", wallet=self.wallet
            )
        return stealth_addr

    def list_stealth_addresses(self):
        if self.wallet is None:
            raise ValueError("Wallet not set")
        return self.rpc_cli.callrpc("liststealthaddresses", wallet=self.wallet)

    def list_unspent(self) -> list:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        return self.rpc_cli.callrpc("listunspent", wallet=self.wallet)

    def list_unspent_anon(self) -> list:
        if self.wallet is None:
            raise ValueError("Wallet not set")
        return self.rpc_cli.callrpc("listunspentanon", wallet=self.wallet)

    def list_wallets(self) -> list:
        return self.rpc_cli.callrpc("listwallets")

    def convertFromSat(self, value):
        sat_readable = value / 10**8
        return sat_readable

    def convertToSat(self, value):
        sat_readable = value * 10**8
        return sat_readable

    def isCsOut(self, scriptPubKey):
        if not (len(scriptPubKey) % 2) == 0:
            return False

        script_hex = unhexlify(scriptPubKey)

        return (
            len(script_hex) == 66
            and script_hex[0] == 0xB8
            and script_hex[1] == 0x63
            and script_hex[2] == 0x76
            and script_hex[3] == 0xA9
            and script_hex[4] == 0x14
            and script_hex[25] == 0x88
            and script_hex[26] == 0xAC
            and script_hex[27] == 0x67
            and script_hex[28] == 0x76
            and script_hex[29] == 0xA8
            and script_hex[30] == 0x20
            and script_hex[63] == 0x88
            and script_hex[64] == 0xAC
            and script_hex[65] == 0x68
        )

    def batch_tx(self, lst, batch_size):
        """Yield successive batches of specified size from lst."""
        for i in range(0, len(lst), batch_size):
            yield lst[i : i + batch_size]


def main() -> None:
    rpc_cli: RpcClientCLI = RpcClientCLI(
        cli_bin="./ghost-cli" if OS != "Windows" else "./ghost-cli.exe",
        data_dir=ghost_data_dir,
        daemon_conf=os.path.join(ghost_data_dir, "ghost.conf"),
    )
    ConsolidateUTXOs(rpc_cli)


if __name__ == "__main__":
    main()
