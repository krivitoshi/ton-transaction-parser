import requests
import binascii
import math
import logging
from typing import List, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import os


load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))
TOKEN = os.getenv("BOT_TOKEN")


logging.basicConfig(level=logging.INFO)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}
MAX_TRANSACTIONS = 100


class ParseStates(StatesGroup):
    waiting_for_address = State()
    waiting_for_count = State()
    waiting_for_min_balance = State()


def crc16(data):
    poly = 0x1021
    reg = 0
    message = bytes(data) + bytes(2)

    for byte in message:
        mask = 0x80
        while mask > 0:
            reg <<= 1
            if byte & mask:
                reg += 1
            mask >>= 1
            if reg > 0xffff:
                reg &= 0xffff
                reg ^= poly

    return bytearray([math.floor(reg / 256), reg % 256])


class TonUtils:
    @staticmethod
    def raw_to_base64(address, bounceable):
        work_chain, hash_part = address.split(":")

        o = bytearray.fromhex(hash_part)  
        d = bytearray(34)

        d[0] = 17 if bounceable else 81
        d[1] = int(work_chain)
        d[2:] = o  

        _ = bytearray(d) + crc16(d) 
        return binascii.b2a_base64(_).decode().replace('+', '-').replace('/', '_').rstrip()

    @staticmethod
    def base64_to_raw(address):
        raw_address = binascii.a2b_base64(address.replace('-', '+').replace('_', '/'))
        work_chain = raw_address[1]
        hash_part = binascii.hexlify(raw_address[2:34]).decode()

        return f"{work_chain}:{hash_part}"


class TonViewerClient:
    def __init__(self):
        self.session = requests.session()
        self.session.headers.update(HEADERS)

    def __parse_transactions(self, address, count, from_lt=None):
        params = {
            "limit": count,
            "before_lt": from_lt
        }

        response = self.session.get(
            url=f"https://tonapi.io/v2/accounts/{address}/events",
            params=params
        ).json()

        return response

    def get_transactions(self, address: str, count: int):
        if ":" not in address:
            base64_address = address
        else:
            base64_address = TonUtils.raw_to_base64(address, 0)

        response = self.session.get(
            url=f"https://tonviewer.com/{base64_address}"
        ).text

        token = response.split("\"authClientToken\":\"")[1].split("\"}")[0]
        self.session.headers.update({
            "Authorization": f"Bearer {token}"
        })

        if count <= MAX_TRANSACTIONS:
            return self.__parse_transactions(address, count)["events"]

        transactions = []
        parsed = 0
        last_lt = None

        while parsed != count:
            to_parse = count - parsed
            if to_parse > MAX_TRANSACTIONS:
                to_parse = MAX_TRANSACTIONS

            response = self.__parse_transactions(address, to_parse, last_lt)
            new_transactions = response["events"]
            transactions += new_transactions
            last_lt = response["next_from"]
            parsed += len(new_transactions)

        return transactions

    def get_balance(self, address: str):
        response = self.session.get(
            url=f"https://tonviewer.com/{address}"
        ).text

        balance = response.split(" <!-- -->$")[1].split("</div>")[0]
        return float(balance.replace(",", ""))


async def return_transactions(source_address: str, transactions_count: int = 10, min_balance: float = 0) -> List[str]:
    client = TonViewerClient()
    transactions = client.get_transactions(source_address, transactions_count)
    transactions_info_list = []
    already_checked = []
    try:
        for transaction in transactions:
            for action in transaction["actions"]:
                sender_address = action["simple_preview"]["accounts"][0]["address"]
                bounceable = action["simple_preview"]["accounts"][0]["is_wallet"]
                amount = action["simple_preview"]["value"]

                if sender_address in already_checked:
                    continue
                already_checked.append(sender_address)

                base64_address = TonUtils.raw_to_base64(sender_address, bounceable)

                try:
                    balance = client.get_balance(base64_address)
                except IndexError:
                    continue

                if balance < min_balance:
                    continue

                transactions_info_list.append(f"{base64_address} | ${balance} | {amount}")
    except:
        return ['транзакции не найдены'] 
    return transactions_info_list


router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Я бот для парсинга транзакций TON. Используйте /parse для начала.")


@router.message(Command("parse"))
async def cmd_parse(message: Message, state: FSMContext):
    await message.answer("Введите адрес для парса транзакций:")
    await state.set_state(ParseStates.waiting_for_address)


@router.message(ParseStates.waiting_for_address)
async def process_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text)
    await message.answer("Введите кол-во транзакций для парса:")
    await state.set_state(ParseStates.waiting_for_count)


@router.message(ParseStates.waiting_for_count)
async def process_count(message: Message, state: FSMContext):
    try:
        count = int(message.text)
        await state.update_data(count=count)
        await message.answer("Введите минимальный баланс в долларах:")
        await state.set_state(ParseStates.waiting_for_min_balance)
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число. Попробуйте снова:")


@router.message(ParseStates.waiting_for_min_balance)
async def process_min_balance(message: Message, state: FSMContext):
    try:
        min_balance = float(message.text)
        user_data = await state.get_data()
        
        await message.answer("Начинаю парсить транзакции...")
        
        transactions = await return_transactions(
            user_data["address"], 
            user_data["count"], 
            min_balance
        )
        
        if transactions:
            result = "\n".join(transactions)
            await message.answer(f"Результаты парсинга:\n{result}")
        else:
            await message.answer("Не найдено транзакций, соответствующих критериям.")
        
        await state.clear()
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число. Попробуйте снова:")


async def main():
    bot = Bot(token=TOKEN)
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
