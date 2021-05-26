import asyncio
import concurrent
import threading
import logging
import json
import ledgerx

import datetime as dt

from ledgerx.util import unique_values_from_key

class MarketState:

    # Constant static variables
    risk_free = 0.005 # 0.5% risk free interest
    timezone = dt.timezone.utc
    strptime_format = "%Y-%m-%d %H:%M:%S%z"
    seconds_per_year = 3600.0 * 24.0 * 365.0  # ignore leap year, okay?

    def __init__(self, skip_expired : bool = True):
        self.last_trade = None
        self.clear()
        self.skip_expired = skip_expired
        self.action_queue = None
        

    def clear(self):
        self.all_contracts = dict()           # dict (contract_id: contract)
        self.traded_contract_ids = dict()     # dict (contract_id: traded-contract)
        self.expired_contracts = dict()       # dict (contract_id: expired-contract)
        self.contract_positions = dict()      # my positions by contract (no lots) dict(contract_id: position)
        self.accounts = dict()                # dict(asset: dict(available_balance: 0, position_locked_amount: 0, ...))
        self.exp_dates = list()               # sorted list of all expiration dates in the market
        self.exp_strikes = dict()             # dict(exp_date : dict(asset: [sorted list of strike prices (int)]))
        self.orders = dict()                  # ALL orders in the market dict{contract_id: dict{mid: order}}
        self.book_states = dict()             # all books in the market  dict{contract_id : dict{mid : book_state}}
        self.book_top = dict()                # all top books in the market dict{contract_id : top}
        if self.last_trade is None:
            self.last_trade = dict()          # last observed trade for a contract dict(contract_id: action)
        self.to_update_basis = dict()         # the set of detected stale positions requiring updates dict(contract_id: position)
        self.label_to_contract_id = dict()    # dict(contract['label']: contract_id)
        self.put_call_map = dict()            # dict(contract_id: contract_id) put -> call and call -> put
        self.costs_to_close = dict()          # dict(contract_id: dict(net, cost, basis, size, bid, ask, low, high))
        self.next_day_contracts = dict()      # dict(asset: next_day_contract)
        self.skip_expired = True              # if expired contracts should be ignored (for positions and cost-basis)
        self.last_heartbeat = None            # the last heartbeat - to detect restarts and network issue
        self.mpid = None                      # the trader id
        self.cid = None                       # the customer/account id

    def mid(self, bid, ask):
        if bid is None and ask is None:
            return None
        elif bid is not None:
            if ask is not None:
                return (bid + ask) /2
            else:
                return bid
        else:
            return ask
    
    def get_book_top(self, contract_id, blocking = False):
        if contract_id is None:
            logging.info("No books for None!")
            return None
        if contract_id in self.expired_contracts:
            logging.debug(f"Not looking for expired books on {contract_id}")
            return None
        if contract_id not in self.book_top:
            logging.info(f"Need books for {contract_id}")
            if blocking:
                # load books now
                self.load_books(contract_id)
            else:
                if contract_id in self.book_states:
                    del self.book_states[contract_id] # signal to load books in next heartbeat
                return None
        if contract_id not in self.book_top:
            logging.warning(f"No books for {contract_id}")
            return None
        return self.book_top[contract_id]
    
    def cost_to_close(self, contract_id):
        "returns dict(low, high, net, basis, cost, ask, bid, size)"
        logging.debug(f"getting cost to close for {contract_id}")
        
        if contract_id not in self.contract_positions:
            return None
        contract = self.all_contracts[contract_id]
        if self.contract_is_expired(contract):
            return None
        position = self.contract_positions[contract_id]
        size = position['size']
        if size == 0:
            return None
        
        top = self.get_book_top(contract_id, True)
        if top is None:
            return None
        bid = MarketState.bid(top)
        ask = MarketState.ask(top)
        mid = self.mid(bid, ask)
        fee = None
        cost = None
        if mid is not None:
            fee = MarketState.fee(mid, size)
            cost = (fee + mid * size) // 10000
        basis = None
        net = None
        if 'basis' in position:
            basis = position['basis'] // 100
            if size < 0 and ask is not None:
                net = int((fee + ask * size) // 10000 - basis)
            elif bid is not None:
                net = int((fee + bid * size) // 10000 - basis)
        if basis is not None:
            if contract_id not in self.costs_to_close or cost != self.costs_to_close[contract_id]['cost']:
                logging.debug(f"net ${net}: cost ${cost} - basis ${basis} to close {size} of {self.all_contracts[contract_id]['label']} at {bid} to {ask}")
        else:
            if contract_id in self.contract_positions:
                self.to_update_basis[contract_id] = self.contract_positions[contract_id]
            logging.warning(f"No basis for ${cost} to close {size} of {self.all_contracts[contract_id]['label']} at {bid} to {ask}")
        low = None
        high = None
        if size < 0:
            if bid is not None:
                low = (fee + bid * size) // 10000
            if ask is not None:
                high = (fee + ask * size) // 10000
        else:
            if ask is not None:
                low = (fee + ask * size) // 10000
            if bid is not None:
                high= (fee + bid * size) // 10000
        ret = dict(net=net, cost=cost, basis=basis, size=size, bid=bid, ask=ask, fee=fee, low=low, high=high)
        self.costs_to_close[contract_id] = ret
        return ret

    @staticmethod
    def ask(top_book):
        if top_book is not None and 'ask' in top_book:
            ask = top_book['ask']
            if ask is not None and ask != 0:
                return ask
        return None

    @staticmethod
    def bid(top_book):
        if top_book is not None and 'bid' in top_book:
            bid = top_book['bid']
            if bid is not None and bid != 0:
                return bid
        return None

    @staticmethod
    def fee(price, size, price_units = 100):
        # $0.15 per contract or 20% of price whichever is less
        fee_per_contract = price // (5 * price_units) # 20%
        if fee_per_contract >= 15:
            fee_per_contract = 15
        return abs(size) * fee_per_contract

    @staticmethod
    def is_same_option_date(contract_a, contract_b):
        return 'is_call' in contract_a and 'is_call' in contract_b and \
            contract_a['is_call'] == contract_b['is_call'] and \
            contract_a['date_expires'] == contract_b['date_expires'] and \
            contract_a['derivative_type'] == contract_b['derivative_type'] and \
            contract_a['underlying_asset'] == contract_b['underlying_asset']

    def contract_is_expired(self, contract, preemptive_seconds = 15):
        if 'date_expires' not in contract:
            logging.warning(f"invalid contract without expiration: {contract}")
        exp = dt.datetime.strptime(contract['date_expires'], self.strptime_format)
        if (exp - dt.datetime.now(self.timezone)).total_seconds() < preemptive_seconds: # do not risk any last second trades...
            return True
        else:
            return contract['id'] in self.expired_contracts

    def contract_is_live(self, contract):
        if 'date_live' not in contract:
            logging.warning(f"invalid contract without date_live: {contract}")
            return False
        live = dt.datetime.strptime(contract['date_live'], self.strptime_format)
        if (dt.datetime.now(self.timezone) - live).total_seconds() < 0:
            return False
        else:
            return True

    def get_filtered_contracts(self, **kwargs):
        """Returns a list of contracts filtered by any key-value in a contract"""
        return_contracts = []
        for contract_id, contract in self.all_contracts.items():
            match = True
            for key,val in kwargs.items():
                if val is None:
                    continue
                if key not in contract or val != contract[key]:
                    match = False
                    break
            if match:
                return_contracts.append(contract)
        return return_contracts

    def get_all_strikes_like_contract(self, contract_id):
        """Returns a list on the same expiration date, same asset, same type, but possibly different strike price"""
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        l = self.get_filtered_contracts(date_expires=contract['date_expires'], underlying_asset=contract['underlying_asset'], derivative_type=contract['derivative_type'], is_call=contract['is_call'], is_next_day=contract['is_next_day'])
        return l
    
    def is_qualified_covered_call(self, contract_id):
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        if contract['is_call'] == False:
            return False
        exp = dt.datetime.strptime(contract['date_expires'], self.strptime_format)
        days = (exp - dt.datetime.now(self.timezone)).total_seconds() / (3600 * 24)
        if days <= 30:
            return False
        
        next_day_contract = self.get_next_day_swap(contract['underlying_asset'])
        next_day_id = None
        if next_day_contract is not None:
            next_day_id = next_day_contract['id']
        top = self.get_book_top(next_day_id)
        if top is not None:
            bid = MarketState.bid(top)
            ask = MarketState.ask(top)
            fmv = bid
            if ask is not None:
                if bid is not None:
                    fmv = (bid + ask) / 2
            if fmv is not None:
                # get all strikes for this call option
                strikes = []
                for test_id, test_contract in self.all_contracts.items():
                    if MarketState.is_same_option_date(contract, test_contract):
                        strikes.append(test_contract['strike_price'])
                strikes.sort(reverse = True)
                lowest_strike = strikes[0]
                past_fmv = 0
                for strike in strikes:
                    if strike <= fmv:
                        past_fmv += 1
                    if past_fmv <= 1 and days > 30:
                        lowest_strike = strike
                    if past_fmv <= 2 and days > 90:
                        lowest_strike = strike
                if contract['strike_price'] >= lowest_strike:
                    return True
        return False


    def add_expiration_date(self, date):
        assert(date not in self.exp_dates)
        self.exp_dates.append(date)
        self.exp_dates.sort()

    def is_my_order(self, order):
        return self.mpid is not None and 'mpid' in order and self.mpid == order['mpid']

    def replace_existing_order(self, order):
        # replace if clock is larger
        mid = order['mid']
        contract_id = order['contract_id']
        assert(contract_id in self.orders and mid in self.orders[contract_id])
        contract_orders = self.orders[contract_id]
        existing = contract_orders[mid]
        assert(order['contract_id'] in self.all_contracts)
        contract = self.all_contracts[contract_id]
        label = contract['label']
        if existing['clock'] <= order['clock'] and existing['ticks'] < order['ticks']:
            if self.is_my_order(existing) and not self.is_my_order(order):
                logging.warning("Existing order is mine but replacement is not. existing {existing} order {order}, ignoring update")
            else:
                if order['size'] == 0:
                    del contract_orders[mid]
                    logging.debug(f"Deleted existing order of zero size {existing} to {order}")
                else:
                    contract_orders[mid] = order
                    logging.debug(f"Replaced existing order on {label} {existing} to {order}")
        else:
            if existing['ticks'] == order['ticks']:
                logging.debug(f"Got duplicate order on {label} {existing} vs {order}")
            else:
                logging.warning(f"existing order on {label} {existing} is newer {order}, ignoring update")
    
    def insert_new_order(self, order):
        mid = order['mid']
        contract_id = order['contract_id']
        if contract_id not in self.orders:
            self.orders[contract_id] = dict()
        assert(mid not in self.orders)
        label = self.all_contracts[contract_id]['label']
        if self.is_my_order(order):
            assert(contract_id not in self.orders or mid not in self.orders[contract_id])
            logging.info(f"Inserted my new order on {label} order {order}")
        else:
            logging.debug(f"Inserted this 3rd party order on {label} order {order}")
        self.orders[contract_id][mid] = order

    def handle_order(self, order):
        is_my_order = self.is_my_order(order)
        mid = order['mid']
        contract_id = order['contract_id']

        # update the contract if needed
        if contract_id not in self.all_contracts:
            logging.warning(f"unknown contract {contract_id}... Retrieving it")
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        label = contract['label']
        
        if contract_id not in self.orders:
            self.orders[contract_id] = dict()
        contract_orders = self.orders[contract_id]

        status = order['status_type']
        exists = mid in contract_orders
        existing = None
        if exists:
            existing = contract_orders[mid]
            if 'mpid' in order and not is_my_order:
                logging.warning(f"different mpid {self.mpid} for mid {mid} existing {existing} order {order}")
        
        if not exists and status != 200:
            logging.debug(f"traded order had not been tracked yet! {order}")
            self.insert_new_order(order)
            exists = True
            existing = contract_orders[mid]
        
        logging.debug(f"handle_order on {contract_id} {label} {order}")
        if status == 200:
            # A resting order was inserted
            if exists:
                self.replace_existing_order(order)
            else:
                self.insert_new_order(order)
            self.handle_book_state(contract_id, order)
        elif status == 201:
            # a cross (trade) occured            
            if is_my_order:
                # This is my traded order, so track position and basis deltas
                if mid in contract_orders:
                    if 'mpid' in contract_orders[mid]:
                        assert(mid in contract_orders and self.mpid == contract_orders[mid]['mpid'])
                    else:
                        logging.warning(f"How can my order not have my mpid? existing {existing} order {order} mid {mid} {contract_orders}")
                delta_pos = order['filled_size']
                delta_basis = order['filled_size'] * order['filled_price']
                
                if order['is_ask']:
                    # sold
                    logging.info(f"Observed sale of {delta_pos} for ${delta_basis//100} on {contract_id} {label} {order}")
                else:
                    # bought
                    logging.info("Observed purchase of {delta_pos} for ${delta_basis//100} on {contract_id} {label} {order}/mi")

                if order['size'] != 0:
                    logging.info(f"Partial fill Cross trade {delta_pos} ${delta_basis//100} {existing} {order}")
                    self.replace_existing_order(order)
                else:
                    logging.info(f"Full fill Cross trade {delta_pos} ${delta_basis//100} {existing} {order}")
                    del contract_orders[mid]

                #if 'id' in position:
                #    size = position['size']
                #    basis = position['basis']
                #    self.update_position(contract_id, position)
                #    if position['size'] != size or position['basis'] != basis:
                #        logging.warning(f"After refresh of trades, size and/or basis do not agree with approximation: {size} {basis} {position} {order}")

            else:
                logging.debug(f"Updating order for books {order}")
                self.replace_existing_order(order)

            if order['size'] != 0:
                self.handle_book_state(contract_id, order)
            else:
                self.delete_book_state(contract_id, mid)

            self.handle_trade(order)

        elif status == 202:
            # A market order was not filled
            logging.warning(f"dunno how to handle not filled market order on {label} {existing} {order}")
        elif status == 203:
            # cancelled
            if exists:
                logging.debug(f"Deleting cancelled order on {label} {existing} {order}")
                del contract_orders[mid]
            else:
                logging.debug(f"Ignoring untracked cancelled order on {label} {order}")
            # handle copy in book_states, if needed
            self.delete_book_state(contract_id, mid)
        elif status == 300:
            logging.info(f"Acknowledged on {label} {existing} {order}")
        elif status == 610:
            # expired
            logging.info(f"Expired on {label} {existing} {order}")
            if exists:
                del contract_orders[mid]
            self.delete_book_state(contract_id, mid)
        elif status >= 600:
            logging.warning(f"invalid or rejected order {order}")
            if exists:
                del contract_orders[mid]

    def get_top_from_book_state(self, contract_id):
        if contract_id not in self.book_states:
            logging.info(f"need books for {contract_id}")
            self.load_books(contract_id)
        books = self.book_states[contract_id]
        ask = None
        bid = None
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        logging.debug(f"get_top_from_book_state contract_id {contract_id} contract {contract} books {books}")
        clock = -1
        for mid,book in books.items():
            assert(mid == book['mid'])
            is_ask = book['is_ask']
            price = book['price']
            if is_ask:
                if ask is None or ask > price:
                    ask = price
            else:
                if bid is None or bid < price:
                    bid = price
            if clock < book['clock']:
                clock = book['clock']
        book_top = dict(ask= ask, bid= bid, contract_id= contract_id, contract_type= None, clock=clock, type= 'book_top')
        if contract_id not in self.book_top or self.book_top[contract_id]['clock'] < clock:
            self.book_top[contract_id] = book_top
        logging.info(f"Top for {contract_id} {contract['label']} {book_top}")
        return book_top
        
    def handle_book_state(self, contract_id, book_state):
        """{clock": 57906, "entry_id": "81d87376167f400fb6545234600856b2", "is_ask": true, "price": 884000, "size": 1}"""
        logging.debug(f"handle_book_state {contract_id} {book_state}")
        assert('mid' in book_state)
        if contract_id not in self.book_states:
            logging.info(f"Ignoring book state for {contract_id} as no books have been loaded or are loading")
            return
        books = self.book_states[contract_id]
        mid = book_state['mid']
        if mid in books:
            book_order = books[mid]
            if book_state['clock'] < book_order['clock']:
                logging.info(f"Ignoring old book_state={book_state} orig={book_order}")
                return
            for key in book_order.keys():
                if key in book_state:
                    book_order[key] = book_state[key]
        else:
            books[mid] = book_state

    def handle_all_book_states(self, book_states):
        assert('contract_id' in book_states)
        assert('book_states' in book_states)
        contract_id = book_states['contract_id']
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        logging.info(f"Replacing all books for {contract_id}: {self.all_contracts[contract_id]['label']} with {len(book_states['book_states'])} entries")
        # replace any existing states
        self.book_states[contract_id] = dict()
        for state in book_states['book_states']:
            self.handle_book_state(contract_id, state)
        self.get_top_from_book_state(contract_id)
    
    def load_books(self, contract_id):
        logging.info(f"Loading books for {contract_id}")
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        if self.contract_is_expired(contract):
            logging.info(f"Skiping book loading on expired contract {contract}")
            return

        try:
            book_states = ledgerx.BookStates.get_book_states(contract_id)
            self.handle_all_book_states(book_states)
            logging.info(f"Added {len(book_states['book_states'])} open orders for {contract_id}")
        except:
            logging.exception(f"No book states for {contract_id}, perhaps it has (just) expired")
        
    def get_top_book_states(self, contract_id, clock_lag = 0):
        """
        returns (top_bid_book_state, top_ask_book_state, clock_lag), after comparing top with all book states
        refreshing book states, if needed
        """
        top_bid_book_state = None
        top_ask_book_state = None
        top_clock = -1
        if contract_id in self.book_states:
            for mid,book_state in self.book_states[contract_id].items():
                if top_clock < book_state['clock']:
                    top_clock = book_state['clock']
        lag = -1
        if contract_id in self.book_top:
            lag = self.book_top[contract_id]['clock'] - top_clock
            if lag < 0:
                # do not reload books because book_top is behind, trust the book_states
                lag = 0
        if lag < 0 or lag > clock_lag: # avoid excessive book reloading -- allow book_top to be a few clocks ahead
            logging.warning(f"book top is too far away of cached book states by {lag} book_states_top_clock={top_clock} vs book_top={self.book_top[contract_id]}")
            top_clock = None
        if top_clock is None or contract_id not in self.book_top or contract_id not in self.book_states:
            logging.info(f"reloading stale books for {contract_id}")
            self.load_books(contract_id)
        for mid,book_state in self.book_states[contract_id].items():
            if book_state['is_ask']:
                if top_ask_book_state is None or top_ask_book_state['price'] > book_state['price']:
                    top_ask_book_state = dict(**book_state)
            else:
                if top_bid_book_state is None or top_bid_book_state['price'] < book_state['price']:
                    top_bid_book_state = dict(**book_state)
        if top_bid_book_state is None or top_ask_book_state is None:
            logging.info(f"top book states are missing {top_bid_book_state} {top_ask_book_state}")
        return (top_bid_book_state, top_ask_book_state, lag)

    def get_top_book_states_estimate(self, contract_id, max_lag = 5):
        """Returns the top_book_states, but does not force a refresh if the book state is lagging and returns size==1 if it is lagging"""
        top_book_states = self.get_top_book_states(contract_id, max_lag)
        if top_book_states[2] > 2:
            logging.debug(f"book states are stale replacing sizes to 1 {top_book_states}")
            if top_book_states[0] is not None:
                top_book_states[0]['size']=1
            if top_book_states[1] is not None:
                top_book_states[1]['size']=1
        return top_book_states


    def legacy_load_books(self, contract_id):
        logging.info(f"Loading books for {contract_id}")
        if contract_id not in self.all_contracts:
            self.retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        if self.contract_is_expired(contract):
            logging.info(f"Skiping book loading on expired contract {contract}")
            return

        try:
            book_states = ledgerx.BookStates.get_book_states(contract_id)
            self.handle_all_book_states(book_states)
            logging.info(f"Added {len(book_states['book_states'])} open orders for {contract_id}")
        except:
            logging.exception(f"No book states for {contract_id}, perhaps it has (just) expired")
 
    async def async_load_books(self, contract_id):
        logging.info(f"async loading books for {contract_id}")
        if contract_id not in self.all_contracts:
            await self.async_retrieve_contract(contract_id)
        contract = self.all_contracts[contract_id]
        if self.contract_is_expired(contract):
            logging.info(f"Skiping book loading on expired contract {contract}")
            return

        try:
            is_queue_start = False
            if self.action_queue is None:
                self.action_queue = []
                is_queue_start = True
            book_states = await ledgerx.BookStates.async_get_book_states(contract_id)
            self.handle_all_book_states(book_states)
            logging.info(f"Added {len(book_states['book_states'])} open orders for {contract_id}")
            if is_queue_start:
                await self.handle_queued_actions()
        except:
            logging.exception(f"No book states for {contract_id}, perhaps it has (just) expired")
 
    async def async_load_all_books(self, contracts, max_parallel = 60):
        logging.info(f"loading all books for {len(contracts)} and max={max_parallel}")
        if self.action_queue is None:
            self.action_queue = [] # will process queued actions on completion
        logging.info(f"Loading books for {contracts}")
        futures = []
        for contract_id in contracts:
            logging.info(f"loading books for {contract_id}")
            fut = self.async_load_books(contract_id)
            logging.info(f"appending future {fut} for books for {contract_id}")
            futures.append( fut )
        if len(futures) > 0:
            await asyncio.gather( *futures )
        logging.info(f"Done loading all books")
        await self.handle_queued_actions()
    
    def delete_book_state(self, contract_id, mid):
        if contract_id not in self.book_states:
            # do not bother loading the book states
            logging.info(f"Ignoring deleted book on untraced contract {contract_id}")
            return
        if mid in self.book_states[contract_id]:
            logging.debug(f"Removing order from books {self.book_states[contract_id][mid]}")
            del self.book_states[contract_id][mid]

    def get_next_day_swap(self, asset):
        next_day_contract = None
        if asset not in self.next_day_contracts:
            for contract_id, contract in self.all_contracts.items():
                if contract['is_next_day'] and asset == contract['underlying_asset'] and not self.contract_is_expired(contract) and self.contract_is_live(contract):
                    self.next_day_contracts[asset] = contract
                    break
        if asset in self.next_day_contracts:
            next_day_contract = self.next_day_contracts[asset]
            if self.contract_is_expired(next_day_contract, 1):
                next_day_contract = None
            if next_day_contract is not None and not self.contract_is_live(next_day_contract):
                next_day_contract = None
        if next_day_contract is None:
            # get the newest one
            logging.info("Discovering the latest NextDay swap contract")
            contracts = ledgerx.Contracts.list_all()
            for c in contracts:
                contract_id = c['id']
                if contract_id not in self.all_contracts:
                    self.add_contract(c)
                if c['is_next_day'] and c['active'] and not self.contract_is_expired(c) and self.contract_is_live(c):
                    self.next_day_contracts[c['underlying_asset']] = c
                    if asset == c['underlying_asset']:
                        next_day_contract = c
        return next_day_contract


    def add_contract(self, contract):
        if contract['date_expires'] not in self.exp_dates:
            self.add_expiration_date(contract['date_expires'])
        assert(contract['date_expires'] in self.exp_dates)
        contract_id = contract['id']
        if contract_id in self.all_contracts:
            return
        logging.info(f"add_contract: new contract {contract}")
        contract_id = contract['id']
        self.all_contracts[contract_id] = contract

        label = contract['label']
        self.label_to_contract_id[label] = contract_id
        if self.contract_is_expired(contract):
            self.expired_contracts[contract_id] = contract
            logging.info(f"contract is expired {contract}")
            if self.skip_expired:
                return
        asset = contract['underlying_asset']
        test_label = self.to_contract_label(asset, contract['date_expires'], contract['derivative_type'], contract['is_call'], contract['strike_price'])
        if label != test_label:
            logging.warning(f"different labels '{label}' vs calculated '{test_label}' for {contract}")
        if contract['is_next_day']:
            logging.info(f"looking at NextDay {contract}")
            if asset not in self.next_day_contracts or not self.contract_is_expired(contract) and contract['active']:
                if asset in self.next_day_contracts:
                    current = self.next_day_contracts[asset]
                    if current['date_expires'] < contract['date_expires']:
                        self.next_day_contracts[asset] = contract
                        logging.info(f"new NextDay swap on {asset} {contract_id} {label}")
                    else:
                        logging.info(f"ignoring old NextDay swap on {asset} {label}")
                else:
                    self.next_day_contracts[asset] = contract
                    logging.info(f"new NextDay swap on {asset} {contract_id} {label}")
            else:
                logging.info(f"already captured old NextDay swap on {asset} {contract_id} {label}")
        if 'Put' in label:
            call_label = label.replace("Put", "Call")
            if call_label in self.label_to_contract_id:
                call_id = self.label_to_contract_id[call_label]
                self.put_call_map[contract_id] = call_id
                self.put_call_map[call_id] = contract_id
                logging.info(f"mapped Put {contract_id} {label} <=> Call {call_id} {call_label}")
            self.add_exp_strike(contract)
        elif 'Call' in label:
            put_label = label.replace("Call", "Put")
            if put_label in self.label_to_contract_id:
                put_id = self.label_to_contract_id[put_label]
                self.put_call_map[contract_id] = put_id
                self.put_call_map[put_id] = contract_id
                logging.info(f"mapped Call {contract_id} {label} <=> Put {put_id} {put_label}")
            self.add_exp_strike(contract)   

    def add_exp_strike(self, contract):
        exp = contract['date_expires']
        assert(exp in self.exp_dates)
        if exp not in self.exp_strikes:
            self.exp_strikes[exp] = dict()
        exp_asset_strikes = self.exp_strikes[exp]
        asset = contract['underlying_asset']
        if asset not in exp_asset_strikes:
            exp_asset_strikes[asset] = []
        exp_strikes = exp_asset_strikes[asset]
        strike = contract['strike_price']
        if strike not in exp_strikes:
            exp_strikes.append(strike)
            exp_strikes.sort()

    def to_contract_label(self, _asset, _exp, derivative_type, is_call = False, strike = None):
        if ' ' in _exp:
            exp = dt.datetime.strptime(_exp, self.strptime_format)
        else:
            exp = dt.datetime.strptime(_exp, self.strptime_format.split(" ")[0])
        exp = exp.strftime("%d%b%Y").upper()
        
        asset = _asset
        multiplier = 1
        if asset == "CBTC":
            asset = "BTC-Mini"
            multiplier = 100
        if derivative_type == 'future_contract':
            return f"{asset}-{exp}-Future"
        elif derivative_type == 'options_contract':
            if is_call:
                return f"{asset}-{exp}-{strike//multiplier}-Call"
            else:
                return f"{asset}-{exp}-{strike//multiplier}-Put"
        elif derivative_type == 'day_ahead_swap':
            return f"{asset}-{exp}-NextDay"
        else:
            logging.warning(f"dunno derivative type {derivative_type}")
            return ""

    def contract_added_action(self, action):
        assert(action['type'] == 'contract_added')
        contract_id = action['data']['id']
        self.retrieve_contract(contract_id, True)
        contract = self.all_contracts[contract_id]
        assert(contract['derivative_type'] == action['data']['derivative_type'])

    def remove_contract(self, contract):
        # just flag it as expired
        assert(contract['date_expires'] in self.exp_dates)
        contract_id = contract['id']
        if contract_id in self.expired_contracts:
            return
        logging.info(f"expired contract {contract}")
        self.expired_contracts[contract_id] = contract

    def contract_removed_action(self, action):
        assert(action['type'] == 'contract_removed')
        self.remove_contract(action['data'])
            
    def trade_busted_action(self, action):
        logging.info("Busted trade {action}")
        # TODO 

    async def open_positions_action(self, action):
        logging.info(f"Positions {action}")
        assert(action['type'] == 'open_positions_update')
        assert('positions' in action)
        update_basis = []
        update_all = []
        for position in action['positions']:
            contract_id = position['contract_id']
            if contract_id in self.contract_positions:
                contract_position = self.contract_positions[contract_id]
                if 'mpid' in contract_position:
                    assert(position['mpid'] == contract_position['mpid'])
                if position['size'] != contract_position['size']:
                    update_basis.append(contract_id)
                for field in ['exercised_size', 'size']:
                    contract_position[field] = position[field]
            elif position['size'] != 0 or position['exercised_size'] != 0:
                self.contract_positions[contract_id] = position
                update_all.append(contract_id)
                logging.info(f"No position for {contract_id}")
        if len(update_all) > 0:
            logging.info(f"Getting new positions for at least these new contracts {update_all}")
            needs_all = False
            for contract_id in update_all:
                if contract_id not in self.contract_positions:
                    needs_all = True
                else:
                    await self.async_update_position(contract_id)
            if needs_all:
                logging.warning(f"Need all positions refreshed")
                await self.async_update_all_positions()
                
        if len(update_basis) > 0:
            logging.info(f"Getting updated basis for these contracts {update_basis}")
            futures = []
            for contract_id in update_basis:
                future = self.async_update_position(contract_id)
                futures.append(future)
            if len(futures) > 0:
                await asyncio.gather( *futures )

    def collateral_balance_action(self, action):
        logging.info(f"Collateral {action}")
        assert(action['type'] == 'collateral_balance_update')
        assert('collateral' in action)
        assert('available_balances' in action['collateral'])
        assert('position_locked_balances' in action['collateral'])
        for balance, asset_balance in action['collateral'].items():
            for asset, val in asset_balance.items():
                if balance not in self.accounts:
                    self.accounts[balance] = dict()
                self.accounts[balance][asset] = val

    async def book_top_action(self, action) -> bool:
        assert(action['type'] == 'book_top')
        contract_id = action['contract_id']
        if contract_id == 0:
            logging.warning(f"Got erroneous book_top {action}")
            return False
        if contract_id not in self.all_contracts:
            logging.info(f"loading contract for book_top {contract_id} {action}")
            await self.async_retrieve_contract(contract_id)
            await self.async_load_books(contract_id)
            logging.info(f"ignoring possible stale book top {action}")
            return False
        else:
            if contract_id not in self.book_top:
                logging.info(f"no books yet for booktop {contract_id} {action}")
                self.book_top[contract_id] = action
            top = self.book_top[contract_id]
            assert(contract_id == top['contract_id'])
            if top['clock'] < action['clock']:
                logging.debug(f"BookTop update {contract_id} {self.all_contracts[contract_id]['label']} {action}")
                self.book_top[contract_id] = action
                #self.cost_to_close(contract_id)
                return True
            else:
                if top['clock'] == action['clock']:
                    if top['ask'] == action['ask'] and top['bid'] == action['bid']:
                        logging.debug(f"Ignored duplicate book top {action}")
                    else:
                        logging.warning(f"Found DIFFERENT book_top with same clock {top} {action}")
                else:
                    logging.debug(f"Ignored stale book top {action} kept newer {top}")
                return False

    async def heartbeat_action(self, action):
        logging.info(f"Heartbeat {action}")
        assert(action['type'] == 'heartbeat')
        if self.last_heartbeat is None:
            pass
        else:
            if self.last_heartbeat['ticks'] >= action['ticks']:
                logging.warning(f"Out of order heartbeats last={self.last_heartbeat} now={action}")
            if self.last_heartbeat['run_id'] != action['run_id']:
                logging.info("Reloading market state")
                await self.load_market()
        self.last_heartbeat = action

        beat_time = dt.datetime.fromtimestamp(action['timestamp'] // 1000000000, tz=self.timezone)
        now = dt.datetime.now(tz=self.timezone)
        delay = (now - beat_time).total_seconds()
        if delay > 2:
            logging.warning(f"Processed old heartbeat {delay} seconds old {action}")
            # do not perform any more work
            return
        await self.load_remaining_books(20)

    async def action_report_action(self, action):
        logging.debug(f"ActionReport {action}")
        assert(action['type'] == 'action_report')
        self.handle_order(action)

    async def handle_queued_actions(self):
        if self.action_queue is not None:
            logging.info(f"Processing {len(self.action_queue)} queued actions")
            while len(self.action_queue) > 0:
                action = self.action_queue.pop(0)
                await self.handle_action(action, True)
            assert(len(self.action_queue) == 0)
            self.action_queue = None
        logging.info(f"Completed processing queued actions")
            
    async def handle_action(self, action, force_run = False):
        if self.action_queue is not None and not force_run:
            self.action_queue.append(action)
            logging.info(f"queueing action while updating with {len(self.action_queue)} pending")
            return
        type = action['type']
        logging.debug(f"handle_action {type} force_run={force_run}")
        if type == 'book_top':
            await self.book_top_action(action)
        elif type == 'action_report':
            await self.action_report_action(action)
        elif type == 'heartbeat':
            await self.heartbeat_action(action)
        elif type == 'collateral_balance_update':
            self.collateral_balance_action(action)
        elif type == 'open_positions_update':
            await self.open_positions_action(action)
        elif type == 'exposure_reports':
            logging.info(f"Exposure report {action}")
        elif type == 'websocket_starting':
            logging.info(f"Websocket has started {action}, books may be stale and need to be resynced")
            await self.load_market()
        elif type == 'contract_added':
            self.contract_added_action(action)
        elif type == 'contract_removed':
            self.contract_removed_action(action)
        elif type == 'trade_busted':
            self.trade_busted_action(action)
        elif 'contact_' in type:
            logging.info(f"contact change {action}")
        elif '_success' in type:
            logging.info(f"Successful {type}")
        else:
            logging.warning(f"Unknown action type {type}: {action}")

    def retrieve_contract(self, contract_id, force = False):
        contract = ledgerx.Contracts.retrieve(contract_id)["data"]
        assert(contract["id"] == contract_id)
        if force or contract_id not in self.all_contracts:
            logging.info(f"retrieve_contract: new contract {contract}")
            self.add_contract(contract)
        return contract  

    async def async_retrieve_contract(self, contract_id, force = False):
        contract_res = await ledgerx.Contracts.async_retrieve(contract_id)
        contract = contract_res["data"]
        assert(contract["id"] == contract_id)
        if force or contract_id not in self.all_contracts:
            logging.info(f"retrieve_contract: new contract {contract}")
            self.add_contract(contract)
        return contract  

    def set_traded_contracts(self):
        # get the list of my traded contracts
        # this may include inactive / expired contracts
        skipped = 0
        traded_contracts = ledgerx.Contracts.list_all_traded()
        logging.info(f"Got {len(traded_contracts)} traded_contracts")
        for traded in traded_contracts:
            logging.debug(f"traded {traded}")
            contract_id = traded['id']
            if self.skip_expired:
                if contract_id in self.expired_contracts or contract_id not in self.all_contracts:
                    skipped += 1
                    continue
            if contract_id not in self.all_contracts:            
                # look it up
                contract = self.retrieve_contract(contract_id)
                
            self.traded_contract_ids[contract_id] = self.all_contracts[contract_id]
            contract_label = self.all_contracts[contract_id]["label"]
            logging.debug(f"Traded {contract_id} {contract_label}")
        logging.info(f"Done loading traded_contracts -- skipped {skipped} expired ones")
        
    def add_transaction(self, transaction):
        logging.debug(f"transaction {transaction}")
        if transaction['state'] != 'executed':
            logging.warning(f"unknown state for transaction: {transaction}")
            return
        asset = transaction['asset']
        if asset not in self.accounts:
            self.accounts[asset] = {"available_balance": 0, "position_locked_amount": 0, "withdrawal_locked_amount" : 0}
        acct = self.accounts[asset]
        if transaction['debit_post_balance'] is not None:
            deb_field = transaction['debit_account_field_name']
            if deb_field not in acct:
                logging.warning(f"unknown balance type {deb_field}")
                acct[deb_field] = 0
            acct[deb_field] -= transaction['amount']
            assert(-transaction['amount'] == transaction['debit_post_balance'] - transaction['debit_pre_balance'])
        if transaction['credit_post_balance'] is not None:
            cred_field = transaction['credit_account_field_name']
            if cred_field not in acct:
                logging.warning(f"unknown balance type {deb_field}")
                acct[cred_field] = 0
            acct[cred_field] += transaction['amount']
            assert(transaction['amount'] == transaction['credit_post_balance'] - transaction['credit_pre_balance'])

    

    async def async_update_basis(self, contract_id, position):
        if 'id' not in position or 'contract' not in position:
            logging.warning(f"Cannot update basis with an improper position {position}")
            self.to_update_basis[contract_id] = position
            return
        contract = position['contract']
        if contract_id != contract['id']:
            logging.warning(f"Improper match of {contract_id} to {position}")
            return

        if self.skip_expired and self.contract_is_expired(contract):
            logging.info(f"skipping basis update for expired contract {contract['label']}")
            return

        pos_id = position["id"]
        logging.info(f"updating position with trades and basis for {contract_id} {position}")
        trades = await ledgerx.Positions.async_list_all_trades(pos_id)
        self.process_basis_trades(contract, position, trades)

    def update_basis(self, contract_id, position):
        if 'id' not in position or 'contract' not in position:
            logging.warning(f"Cannot update basis with an improper position {position}")
            self.to_update_basis[contract_id] = position
            return
        contract = position['contract']
        if contract_id != contract['id']:
            logging.warning(f"Improper match of {contract_id} to {position}")
            return

        if self.skip_expired and self.contract_is_expired(contract):
            logging.info(f"skipping basis update for expired contract {contract['label']}")
            return

        pos_id = position["id"]
        logging.info(f"updating position with trades and basis for {contract_id} {position}")
        trades = ledgerx.Positions.list_all_trades(pos_id)
        self.process_basis_trades(contract, position, trades)

    def process_basis_trades(self, contract, position, trades):
        contract_id = contract['id']
        contract_label = contract['label']
        logging.info(f"got {len(trades)} trades for {contract_id} {contract_label}")
        pos = 0
        basis = 0
        for trade in trades:
            logging.debug(f"contract {contract_id} trade {trade}")
            assert(contract_id == int(trade["contract_id"]))
            if trade["side"] == "bid":
                # bought so positive basis and position delta
                basis += trade["fee"] - trade["rebate"] + trade["premium"]
                pos += trade["filled_size"]
            else:
                assert(trade["side"] == "ask")
                # sold, so negative basis and negative position delta
                basis += trade["fee"] - trade["rebate"] - trade["premium"]
                pos -= trade["filled_size"]
        #logging.debug(f"final pos {pos} basis {basis} position {position}")
        if position["type"] == "short":
            assert(pos <= 0)
        else:
            assert(position["type"] == "long")
            assert(pos >= 0)
        if pos != position['size']:
            logging.warning(f"update to position did not yield pos={pos} {position}, updating them all")
            self.update_all_positions()
            return
        position["basis"] = basis
        cost = basis / 100.0
        self.contract_positions[contract_id] = position
        if contract_id in self.to_update_basis:
            del self.to_update_basis[contract_id]

        logging.info(f"Position after {len(trades)} trade(s) {position['size']} CBTC ${cost} -- {contract_id} {contract_label}")
        

    async def async_update_all_positions(self):
        logging.info(f"Updating all positions")
        all_positions = await ledgerx.Positions.async_list_all()
        self.process_all_positions(all_positions)

    def update_all_positions(self):
        logging.info(f"Updating all positions")
        all_positions = ledgerx.Positions.list_all()
        self.process_all_positions(all_positions)

    def process_all_positions(self, all_positions):
        logging.info(f"Processing {len(all_positions)} positions")
        for pos in all_positions:
            assert('id' in pos and 'contract' in pos)
            contract = pos['contract']
            contract_id = contract['id']
            old_pos = None
            if contract_id in self.contract_positions:
                old_pos = self.contract_positions[contract_id]
                if 'basis' in old_pos and old_pos['size'] == pos['size'] and old_pos['assigned_size'] == pos['assigned_size']:
                    pos['basis'] = old_pos['basis']
            self.contract_positions[contract_id] = pos
            if 'basis' not in pos:
                if self.skip_expired:
                    if contract_id in self.expired_contracts or contract_id not in self.all_contracts:
                        continue
                logging.info(f"position for {contract_id} {contract['label']} is missing basis or changed {pos}")
                self.to_update_basis[contract_id] = pos

    async def async_update_position(self, contract_id, position = None):
        logging.info(f"async update positions {contract_id} {position}")
        if position is None or 'id' not in position:
            await self.async_update_all_positions()
            position = self.contract_positions[contract_id]
        self.update_position(contract_id, position, False)
        await self.async_update_basis(contract_id, position)

    def update_position(self, contract_id, position = None, update_basis_too = True):
        logging.info(f"updating position for {contract_id}")
        if contract_id not in self.all_contracts:
            if self.skip_expired and contract_id in self.expired_contracts:
                return
            self.retrieve_contract(contract_id)
        if position is None and contract_id in self.contract_positions:
            position = self.contract_positions[contract_id]
        if position is None or 'id' not in position:
            logging.warning(f"listing all positions as it is missing for {contract_id}")
            self.update_all_positions()
            if contract_id not in self.contract_positions:
                logging.warning(f"After updating all, still could not find a position for {contract_id}")
                return
            position = self.contract_positions[contract_id]
        if position is None or 'id' not in position:
            logging.warning(f"Could not find a postiion for {contract_id}")
            return
        
        if update_basis_too:
            self.update_basis(contract_id, position)
        
    async def load_market(self):
        logging.info(f"Loading the Market")
        self.clear()

        if self.action_queue is None:
            self.action_queue = [] # load_positions_orders_and_books will process queued actions
        
        # first load all active contracts, dates and meta data
        logging.info("Loading contracts")
        contracts = ledgerx.Contracts.list_all()
        self.exp_dates = unique_values_from_key(contracts, "date_expires")
        self.exp_dates.sort()
        logging.info(f"Got {len(self.exp_dates)} Expiration dates ")
        for d in self.exp_dates:
            logging.info(f"{d}")
        
        for contract in contracts:
            self.add_contract(contract)
        logging.info(f"Found {len(self.all_contracts.keys())} Contracts")

        # load my open orders
        self.orders.clear()
        num = 0
        for order in ledgerx.Orders.open()['data']:
            if self.mpid is None:
                self.mpid = order['mpid']
            if self.cid is None:
                self.cid = order['cid']
            assert(self.mpid == order['mpid'])
            assert(self.cid == order['cid'])
            logging.debug(f"open order {order}")
            self.handle_order(order)
            num += 1
        logging.info(f"Found {num} Open Order over {len(self.orders.keys())} contracts")

        # load the set of contracts traded in my account
        self.set_traded_contracts()

        await self.load_positions_orders_and_books()

    async def load_all_transactions(self):
        # load transactions for and get account balances
        logging.info("Loading transactions for account balances")
        transactions = ledgerx.Transactions.list_all()
        for transaction in transactions:
            self.add_transaction(transaction)
        logging.info(f"Loaded {len(transactions)} transactions")
        logging.info(f"Accounts: {self.accounts}")
           
    async def load_positions_orders_and_books(self):
        logging.info("Loading positions orders and books")

        if self.action_queue is None:
            self.action_queue = [] # async_load_all_books will process queued actions

        # TODO is this still needed? --- await self.load_all_transactions()

        # get the positions for the my traded contracts
        #expired_contracts = await ledgerx.Contracts.async_list_all(dict(active = 'False'))
        #logging.info(f"Got {len(expired_contracts)} Expired contracts")
        #for contract in expired_contracts:
        #    self.add_contract(contract)
        #    self.expired_contracts[contract['id']] = contract

        await self.async_update_all_positions()

        await self.async_load_all_books(list(self.all_contracts.keys()))

        # get all the trades for each of my positions
        # and calculate basis & validate the balances
        futures = []
        for contract_id, position in self.contract_positions.items():
            if self.skip_expired and contract_id not in self.all_contracts:
                continue
            future = self.async_update_position(contract_id, position)
            futures.append(future)
        if len(futures) > 0:
            await asyncio.gather( *futures )
                        
        if not self.skip_expired:
            # zero out expired positions -- they no longer exist
            for contract_id, expired in self.expired_contracts.items():
                if contract_id in self.contract_positions:
                    position = self.contract_positions[contract_id]
                    position['expired_size'] = position['size']
                    position['size'] = 0
                    logging.info(f"Adjusted expired position {position}")

        
        open_contracts = list(self.contract_positions.keys())
        open_contracts.sort()
        logging.info(f"Have the following {len(open_contracts)} Open Positions")
        for contract_id in open_contracts:
            if self.skip_expired and contract_id not in self.all_contracts:
                continue
            contract = self.all_contracts[contract_id]
            label = contract['label']
            position = self.contract_positions[contract_id]
            if position['size'] == 0:
                continue
            cost = None
            if 'basis' in position:
                cost = position['basis'] / 100.0
            logging.info(f"{label} {position['size']} {cost}")

        self.net_cost_to_close_all()

    def net_cost_to_close_all(self):
        # Calculate net to close all positions 
        logging.info(f"Calculating costs to close all positions")
        total_net_close = 0
        total_net_basis = 0
        for contract_id, position in self.contract_positions.items():
            if self.skip_expired and contract_id not in self.all_contracts:
                continue
            label = self.all_contracts[contract_id]['label']
            basis = None
            size = position['size']
            if 'basis' in position:
                basis = position['basis']
                total_net_basis += basis
            top = self.get_book_top(contract_id)
            if top is not None:
                if size > 0:
                    # sell at bid
                    bid = MarketState.bid(top)
                    if bid is not None:
                        fee = MarketState.fee(bid,size)
                        sale = (size * bid - fee) // 10000
                        total_net_close += sale
                        logging.info(f"Sell for ${sale}, {size} of {label} at top bid ${bid//100} with basis ${basis//100}, net ${(sale - basis//100)//1}")
                    else:
                        logging.info(f"No bid buyers for {size} of {label}")
                elif size < 0:
                    # buy at ask
                    ask = MarketState.ask(top)
                    if ask is not None:
                        fee = MarketState.fee(ask,size)
                        purchase = (size * ask + fee) // 10000
                        total_net_close += purchase
                        logging.info(f"Buy for ${-purchase}, {-size} of {label} at top ask ${ask//100} with basis ${basis//100}, net ${(purchase - basis/100)//1}")
                    else:
                        logging.info(f"No ask sellers for {size} of {label}")
        logging.info(f"Net to close ${total_net_close} with basis ${total_net_basis//100} = ${total_net_close - total_net_basis//100} to close all positions at best (top) price.  Did not explore all books for size")

    def handle_trade(self, action_report):
        logging.info(f"looking if last trade is {action_report}")
        contract_id = int(action_report['contract_id'])
        last = None
        test = dict(id=None, contract_id=contract_id, order_type=action_report['order_type'], \
                filled_price=action_report['filled_price'], filled_size=action_report['filled_size'], \
                timestamp=int(action_report['updated_time']), contract_label=self.all_contracts[contract_id]['label'], \
                side='ask' if action_report['is_ask'] else 'bid')
        if 'mpid' in action_report:
            test['mpid'] = action_report['mpid']
        if contract_id in self.last_trade:
            last = self.last_trade[contract_id]
        if last is None or test['timestamp'] > last['timestamp']:
            logging.info(f"Updated last trade on {contract_id} from {last} to {test} last_trade is {id(self.last_trade)} with {len(self.last_trade)}")
            self.last_trade[contract_id] = test

    def get_last_trade(self, contract_id):
        if contract_id in self.last_trade:
            return self.last_trade[contract_id]
        else:
            logging.info(f"No last trade for {contract_id} last_trade is {id(self.last_trade)} with {len(self.last_trade)}")
            return None
    
    def process_trades(self, trades):
        logging.info(f"Processing {trades} last_trade is currently {id(self.last_trade)} with {len(self.last_trade)}")
        for trade in trades:
            # fix strings taht should be ints
            trade['contract_id'] = contract_id = int(trade['contract_id'])
            trade['timestamp'] = int(trade['timestamp'])
            last = None
            if contract_id in self.last_trade:
                last = self.last_trade[contract_id]
            if last is not None and trade['timestamp'] < last['timestamp']:
                continue
            logging.info(f"Processed last trade for {contract_id} from {last} to {trade}")
            self.last_trade[contract_id] = trade
        logging.info(f"last trade is now {id(self.last_trade)} with {len(self.last_trade)}")

    async def load_latest_trades(self, past_minutes = 60*5):
        logging.info(f"Loading latest trades")
        before_date = dt.datetime.now(self.timezone)
        after_date = before_date - dt.timedelta(minutes=past_minutes)
        before_date = before_date.strftime('%Y-%m-%dT%H:%M')
        after_date = after_date.strftime('%Y-%m-%dT%H:%M')
        logging.info(f"Initiating request for trades in the past {past_minutes} minutes {after_date} to {before_date}")
        await ledgerx.Trades.async_list_all_incremental_return(dict(after_ts=after_date,before_ts=before_date),self.process_trades)
        logging.info(f"Finished loading past trades")

    async def load_remaining_books(self, max = 20):
        futures = []
        count = 0
        to_update = list(self.to_update_basis.items())
        for contract_id,pos in to_update:
            logging.info(f"requested update basis on {contract_id} {pos}")
            if 'id' in pos and 'contract' in pos:
                futures.append( self.async_update_basis(contract_id, pos) )
            else:
                futures.append( self.async_update_position(contract_id) )
            if contract_id in self.to_update_basis:
                del self.to_update_basis[contract_id]
            count = count + 1
            if max > 0 and count >= max:
                    break
        if count > 0:
            logging.info(f"Updating {count} position basis")
        
        if max == 0 or count < max:
            for contract_id, contract in self.all_contracts.items():
                if self.contract_is_expired(contract):
                    continue
                if contract_id not in self.book_states:
                    logging.info(f"Loading books for {contract_id}")
                    futures.append(self.async_load_books(contract_id))
                    count = count + 1
                    if max > 0 and count >= max:
                        break
        if len(futures) > 0:
            await asyncio.gather( *futures )
        
        if count > 0:
            logging.info(f"Done loading {count} of {len(to_update)} stale positions and books")

    def _run_websocket_server(self, callback, include_api_key, repeat_server_port):
        logging.info("Running websocket server")
        return ledgerx.WebSocket.run_server(callback, include_api_key=include_api_key, repeat_server_port=repeat_server_port)

    async def __start_websocket_and_run(self, executor, include_api_key=False, repeat_server_port=None):
        loop = asyncio.get_running_loop()
        
        task1 = await loop.run_in_executor(executor, self.load_latest_trades)
        task2 = await loop.run_in_executor(executor, self._run_websocket_server, self.handle_action, include_api_key, repeat_server_port)
        await asyncio.gather( task1, task2 ) 

    def start_websocket_and_run(self, include_api_key=False, repeat_server_port=None):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        logging.info(f"Starting market_state = {self}")
        
        loop = asyncio.get_event_loop()
        threading.Thread(target=loop.run_until_complete, args=(self.__start_websocket_and_run(executor, include_api_key, repeat_server_port),)).start()
