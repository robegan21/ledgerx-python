import os

# settings
API_BASE = "https://api.ledgerx.com"
WEBSOCKET_BASE = "wss://api.ledgerx.com/ws"
LEGACY_API_BASE = "https://trade.ledgerx.com/api"

DELAY_SECONDS = 0.01
DEFAULT_LIMIT = 200


# configurations
api_key = None
verify_ssl_certs = True

# endpoints as classes
from ledgerx.trades import Trades
from ledgerx.contracts import Contracts
from ledgerx.positions import Positions
from ledgerx.transactions import Transactions
from ledgerx.orders import Orders
from ledgerx.book_states import BookStates
from ledgerx.bitvol import Bitvol
from ledgerx.websocket import WebSocket
from ledgerx.market_state import MarketState
