import time
import json
import os
import copy
import pytz
import threading
from threading import Thread, Lock
from datetime import datetime, timedelta

from ...strategy import Strategy
from ...logger import Logger
from live_table_manager import shared_symbols_data

logging = Logger(logger_name="BybitQSStrategy", filename="BybitQSStrategy.log", stream=True)

symbol_locks = {}

class BybitQSStrategy(Strategy):
    def __init__(self, exchange, manager, config, symbols_allowed=None):
        super().__init__(exchange, config, manager, symbols_allowed)
        self.is_order_history_populated = False
        self.last_known_ask = {}  # Dictionary to store last known ask prices for each symbol
        self.last_known_bid = {} 
        self.last_health_check_time = time.time()
        self.health_check_interval = 600
        self.last_long_tp_update = datetime.now()
        self.last_short_tp_update = datetime.now()
        self.next_long_tp_update = self.calculate_next_update_time()
        self.next_short_tp_update = self.calculate_next_update_time()
        self.last_cancel_time = 0
        self.spoofing_active = False
        self.spoofing_wall_size = 5
        self.spoofing_duration = 5
        self.spoofing_interval = 1
        try:
            self.max_usd_value = self.config.max_usd_value
            self.blacklist = self.config.blacklist
            self.test_orders_enabled = self.config.test_orders_enabled
        except AttributeError as e:
            logging.error(f"Failed to initialize attributes from config: {e}")

    def run(self, symbol, rotator_symbols_standardized=None):
        standardized_symbol = symbol.upper()  # Standardize the symbol name
        logging.info(f"Standardized symbol: {standardized_symbol}")
        current_thread_id = threading.get_ident()

        if standardized_symbol not in symbol_locks:
            symbol_locks[standardized_symbol] = threading.Lock()

        if symbol_locks[standardized_symbol].acquire(blocking=False):
            logging.info(f"Lock acquired for symbol {standardized_symbol} by thread {current_thread_id}")
            try:
                self.run_single_symbol(standardized_symbol, rotator_symbols_standardized)
            finally:
                symbol_locks[standardized_symbol].release()
                logging.info(f"Lock released for symbol {standardized_symbol} by thread {current_thread_id}")
        else:
            logging.info(f"Failed to acquire lock for symbol: {standardized_symbol}")

    def run_single_symbol(self, symbol, rotator_symbols_standardized=None):
        logging.info(f"Starting to process symbol: {symbol}")
        logging.info(f"Initializing default values for symbol: {symbol}")

        min_qty = None
        current_price = None
        total_equity = None
        available_equity = None
        one_minute_volume = None
        five_minute_volume = None
        five_minute_distance = None
        trend = None
        long_pos_qty = 0
        short_pos_qty = 0
        long_upnl = 0
        short_upnl = 0
        cum_realised_pnl_long = 0
        cum_realised_pnl_short = 0
        long_pos_price = None
        short_pos_price = None

        # Initializing time trackers for less frequent API calls
        last_equity_fetch_time = 0
        equity_refresh_interval = 1800  # 30 minutes in seconds

        # Clean out orders
        self.exchange.cancel_all_orders_for_symbol_bybit(symbol)

        # Check leverages only at startup
        self.current_leverage = self.exchange.get_current_max_leverage_bybit(symbol)
        self.max_leverage = self.exchange.get_current_max_leverage_bybit(symbol)

        self.exchange.set_leverage_bybit(self.max_leverage, symbol)
        self.exchange.set_symbol_to_cross_margin(symbol, self.max_leverage)

        logging.info(f"Running for symbol (inside run_single_symbol method): {symbol}")

        quote_currency = "USDT"
        max_retries = 5
        retry_delay = 5
        wallet_exposure = self.config.wallet_exposure
        min_dist = self.config.min_distance
        min_vol = self.config.min_volume
        MaxAbsFundingRate = self.config.MaxAbsFundingRate
        
        hedge_ratio = self.config.hedge_ratio

        price_difference_threshold = self.config.hedge_price_difference_threshold

        if self.config.dashboard_enabled:
            dashboard_path = os.path.join(self.config.shared_data_path, "shared_data.json")

        logging.info("Setting up exchange")
        self.exchange.setup_exchange_bybit(symbol)

        previous_five_minute_distance = None

        since_timestamp = int((datetime.now() - timedelta(days=1)).timestamp() * 1000)  # 24 hours ago in milliseconds
        recent_trades = self.fetch_recent_trades_for_symbol(symbol, since=since_timestamp, limit=20)

        logging.info(f"Recent trades for {symbol} : {recent_trades}")

        # Check if there are any trades in the last 24 hours
        recent_activity = any(trade['timestamp'] >= since_timestamp for trade in recent_trades)
        if recent_activity:
            logging.info(f"Recent trading activity detected for {symbol}")
        else:
            logging.info(f"No recent trading activity for {symbol} in the last 24 hours")


        while True:
            current_time = time.time()
            logging.info(f"Max USD value: {self.max_usd_value}")

            # Log which thread is running this part of the code
            thread_id = threading.get_ident()
            logging.info(f"[Thread ID: {thread_id}] In while true loop {symbol}")

            # Fetch open symbols every loop
            open_position_data = self.retry_api_call(self.exchange.get_all_open_positions_bybit)

            logging.info(f"Open position data: {open_position_data}")

            position_details = {}

            for position in open_position_data:
                info = position.get('info', {})
                position_symbol = info.get('symbol', '').split(':')[0]  # Use a different variable name

                # Ensure 'size', 'side', and 'avgPrice' keys exist in the info dictionary
                if 'size' in info and 'side' in info and 'avgPrice' in info:
                    size = float(info['size'])
                    side = info['side'].lower()
                    avg_price = float(info['avgPrice'])

                    # Initialize the nested dictionary if the position_symbol is not already in position_details
                    if position_symbol not in position_details:
                        position_details[position_symbol] = {'long': {'qty': 0, 'avg_price': 0}, 'short': {'qty': 0, 'avg_price': 0}}

                    # Update the quantities and average prices based on the side of the position
                    if side == 'buy':
                        position_details[position_symbol]['long']['qty'] += size
                        position_details[position_symbol]['long']['avg_price'] = avg_price
                    elif side == 'sell':
                        position_details[position_symbol]['short']['qty'] += size
                        position_details[position_symbol]['short']['avg_price'] = avg_price
                else:
                    logging.warning(f"Missing 'size', 'side', or 'avgPrice' in position info for {position_symbol}")

            open_symbols = self.extract_symbols_from_positions_bybit(open_position_data)
            open_symbols = [symbol.replace("/", "") for symbol in open_symbols]
            logging.info(f"Open symbols: {open_symbols}")
            open_orders = self.retry_api_call(self.exchange.get_open_orders, symbol)

            # position_last_update_time = self.get_position_update_time(symbol)

            # logging.info(f"{symbol} last update time: {position_last_update_time}")

            # Fetch equity data less frequently or if it's not available yet
            if current_time - last_equity_fetch_time > equity_refresh_interval or total_equity is None:
                total_equity = self.retry_api_call(self.exchange.get_balance_bybit, quote_currency)
                available_equity = self.retry_api_call(self.exchange.get_available_balance_bybit, quote_currency)
                last_equity_fetch_time = current_time

                logging.info(f"Total equity: {total_equity}")
                logging.info(f"Available equity: {available_equity}")
                
                # If total_equity is still None after fetching, log a warning and skip to the next iteration
                if total_equity is None:
                    logging.warning("Failed to fetch total_equity. Skipping this iteration.")
                    time.sleep(10)  # wait for a short period before retrying
                    continue

            blacklist = self.config.blacklist
            if symbol in blacklist:
                logging.info(f"Symbol {symbol} is in the blacklist. Stopping operations for this symbol.")
                break

            funding_check = self.is_funding_rate_acceptable(symbol)
            logging.info(f"Funding check on {symbol} : {funding_check}")

            current_price = self.exchange.get_current_price(symbol)

            order_book = self.exchange.get_orderbook(symbol)
            # best_ask_price = self.exchange.get_orderbook(symbol)['asks'][0][0]
            # best_bid_price = self.exchange.get_orderbook(symbol)['bids'][0][0]

            # Handling best ask price
            if 'asks' in order_book and len(order_book['asks']) > 0:
                best_ask_price = order_book['asks'][0][0]
                self.last_known_ask[symbol] = best_ask_price  # Update last known ask price
            else:
                best_ask_price = self.last_known_ask.get(symbol)  # Use last known ask price

            # Handling best bid price
            if 'bids' in order_book and len(order_book['bids']) > 0:
                best_bid_price = order_book['bids'][0][0]
                self.last_known_bid[symbol] = best_bid_price  # Update last known bid price
            else:
                best_bid_price = self.last_known_bid.get(symbol)  # Use last known bid price
                            
            logging.info(f"Open symbols: {open_symbols}")
            logging.info(f"Current rotator symbols: {rotator_symbols_standardized}")
            symbols_to_manage = [s for s in open_symbols if s not in rotator_symbols_standardized]
            logging.info(f"Symbols to manage {symbols_to_manage}")
            
            #logging.info(f"Open orders for {symbol}: {open_orders}")

            logging.info(f"Symbols allowed: {self.symbols_allowed}")

            trading_allowed = self.can_trade_new_symbol(open_symbols, self.symbols_allowed, symbol)
            logging.info(f"Checking trading for symbol {symbol}. Can trade: {trading_allowed}")
            logging.info(f"Symbol: {symbol}, In open_symbols: {symbol in open_symbols}, Trading allowed: {trading_allowed}")

            self.initialize_symbol(symbol, total_equity, best_ask_price, self.max_leverage)

            with self.initialized_symbols_lock:
                logging.info(f"Initialized symbols: {list(self.initialized_symbols)}")


            moving_averages = self.get_all_moving_averages(symbol)

            # self.check_for_inactivity(long_pos_qty, short_pos_qty)

            time.sleep(10)

            logging.info(f"Rotator symbols standardized: {rotator_symbols_standardized}")

            # If the symbol is in rotator_symbols and either it's already being traded or trading is allowed.
            if symbol in rotator_symbols_standardized or (symbol in open_symbols or trading_allowed): # and instead of or

                # Fetch the API data
                api_data = self.manager.get_api_data(symbol)

                # Extract the required metrics using the new implementation
                metrics = self.manager.extract_metrics(api_data, symbol)

                # Assign the metrics to the respective variables
                one_minute_volume = metrics['1mVol']
                five_minute_volume = metrics['5mVol']
                one_minute_distance = metrics['1mSpread']
                five_minute_distance = metrics['5mSpread']
                trend = metrics['Trend']
                mfirsi_signal = metrics['MFI']
                funding_rate = metrics['Funding']
                hma_trend = metrics['HMA Trend']
                eri_trend = metrics['ERI Trend']

                position_data = self.retry_api_call(self.exchange.get_positions_bybit, symbol)

                long_pos_qty = position_details.get(symbol, {}).get('long', {}).get('qty', 0)
                short_pos_qty = position_details.get(symbol, {}).get('short', {}).get('qty', 0)

                logging.info(f"Rotator symbol trading: {symbol}")
                            
                logging.info(f"Rotator symbols: {rotator_symbols_standardized}")
                logging.info(f"Open symbols: {open_symbols}")

                logging.info(f"Long pos qty {long_pos_qty} for {symbol}")
                logging.info(f"Short pos qty {short_pos_qty} for {symbol}")

                # short_liq_price = position_data["short"]["liq_price"]
                # long_liq_price = position_data["long"]["liq_price"]

                # Initialize the symbol and quantities if not done yet
                self.initialize_symbol(symbol, total_equity, best_ask_price, self.max_leverage)

                with self.initialized_symbols_lock:
                    logging.info(f"Initialized symbols: {list(self.initialized_symbols)}")

                self.set_position_leverage_long_bybit(symbol, long_pos_qty, total_equity, best_ask_price, self.max_leverage)
                self.set_position_leverage_short_bybit(symbol, short_pos_qty, total_equity, best_ask_price, self.max_leverage)

                # Update dynamic amounts based on max trade quantities
                self.update_dynamic_amounts(symbol, total_equity, best_ask_price)

                long_dynamic_amount, short_dynamic_amount, min_qty = self.calculate_dynamic_amount_v2(symbol, total_equity, best_ask_price, self.max_leverage)


                logging.info(f"Long dynamic amount: {long_dynamic_amount} for {symbol}")
                logging.info(f"Short dynamic amount: {short_dynamic_amount} for {symbol}")


                self.print_trade_quantities_once_bybit(symbol)

                logging.info(f"Tried to print trade quantities")

                with self.initialized_symbols_lock:
                    logging.info(f"Initialized symbols: {list(self.initialized_symbols)}")


                short_upnl = position_data["short"]["upnl"]
                long_upnl = position_data["long"]["upnl"]
                cum_realised_pnl_long = position_data["long"]["cum_realised"]
                cum_realised_pnl_short = position_data["short"]["cum_realised"]

                # Get the average price for long and short positions
                long_pos_price = position_details.get(symbol, {}).get('long', {}).get('avg_price', None)
                short_pos_price = position_details.get(symbol, {}).get('short', {}).get('avg_price', None)

                short_take_profit = None
                long_take_profit = None

                short_take_profit, long_take_profit = self.calculate_take_profits_based_on_spread(short_pos_price, long_pos_price, symbol, five_minute_distance, previous_five_minute_distance, short_take_profit, long_take_profit)
                previous_five_minute_distance = five_minute_distance


                logging.info(f"Short take profit for {symbol}: {short_take_profit}")
                logging.info(f"Long take profit for {symbol}: {long_take_profit}")

                should_short = self.short_trade_condition(best_ask_price, moving_averages["ma_3_high"])
                should_long = self.long_trade_condition(best_bid_price, moving_averages["ma_3_low"])
                should_add_to_short = False
                should_add_to_long = False

                if short_pos_price is not None:
                    should_add_to_short = short_pos_price < moving_averages["ma_6_low"] and self.short_trade_condition(best_ask_price, moving_averages["ma_6_high"])

                if long_pos_price is not None:
                    should_add_to_long = long_pos_price > moving_averages["ma_6_high"] and self.long_trade_condition(best_bid_price, moving_averages["ma_6_low"])

                open_tp_order_count = self.exchange.bybit.get_open_tp_order_count(symbol)

                logging.info(f"Open TP order count {open_tp_order_count}")

                if self.test_orders_enabled and current_time - self.last_cancel_time >= self.spoofing_interval:
                    self.spoofing_active = True
                    self.helperv2(symbol, short_dynamic_amount, long_dynamic_amount)
                
                logging.info(f"Five minute volume for {symbol} : {five_minute_volume}")
                    

                self.bybit_qs_entry_exit_eri(open_orders,
                                             symbol,
                                             trend,
                                             mfirsi_signal,
                                             eri_trend,
                                             five_minute_volume,
                                             five_minute_distance,
                                             min_vol,
                                             min_dist,
                                             long_dynamic_amount,
                                             short_dynamic_amount,
                                             long_pos_qty,
                                             short_pos_qty,
                                             long_pos_price,
                                             short_pos_price,
                                             should_long,
                                             should_short,
                                             should_add_to_long,
                                             should_add_to_short,
                                             hedge_ratio,
                                             price_difference_threshold
                                            )
                    
                # self.bybit_entry_mm_5m_with_qfl_mfi_and_auto_hedge_with_eri(open_orders, symbol, trend, hma_trend, mfirsi_signal, eri_trend, five_minute_volume, five_minute_distance, min_vol, min_dist, long_dynamic_amount, short_dynamic_amount, long_pos_qty, short_pos_qty, long_pos_price, short_pos_price, should_long, should_short, should_add_to_long, should_add_to_short, hedge_ratio, price_difference_threshold)

                tp_order_counts = self.exchange.bybit.get_open_tp_order_count(symbol)

                long_tp_counts = tp_order_counts['long_tp_count']
                short_tp_counts = tp_order_counts['short_tp_count']

                logging.info(f"Long tp counts: {long_tp_counts}")
                logging.info(f"Short tp counts: {short_tp_counts}")

                logging.info(f"Long pos qty {long_pos_qty} for {symbol}")
                logging.info(f"Short pos qty {short_pos_qty} for {symbol}")

                logging.info(f"Long take profit {long_take_profit} for {symbol}")
                logging.info(f"Short take profit {short_take_profit} for {symbol}")

                logging.info(f"Long TP order count for {symbol} is {tp_order_counts['long_tp_count']}")
                logging.info(f"Short TP order count for {symbol} is {tp_order_counts['short_tp_count']}")

                # Place long TP order if there are no existing long TP orders
                if long_pos_qty > 0 and long_take_profit is not None and tp_order_counts['long_tp_count'] == 0:
                    logging.info(f"Placing long TP order for {symbol} with {long_take_profit}")
                    self.bybit_hedge_placetp_maker(symbol, long_pos_qty, long_take_profit, positionIdx=1, order_side="sell", open_orders=open_orders)

                # Place short TP order if there are no existing short TP orders
                if short_pos_qty > 0 and short_take_profit is not None and tp_order_counts['short_tp_count'] == 0:
                    logging.info(f"Placing short TP order for {symbol} with {short_take_profit}")
                    self.bybit_hedge_placetp_maker(symbol, short_pos_qty, short_take_profit, positionIdx=2, order_side="buy", open_orders=open_orders)
                    
                current_time = datetime.now()

                # Check for long positions
                if long_pos_qty > 0:
                    if current_time >= self.next_long_tp_update and long_take_profit is not None:
                        self.next_long_tp_update = self.update_take_profit_spread_bybit(
                            symbol=symbol, 
                            pos_qty=long_pos_qty, 
                            long_take_profit=long_take_profit,
                            short_take_profit=short_take_profit,
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            positionIdx=1, 
                            order_side="sell", 
                            next_tp_update=self.next_long_tp_update,
                            five_minute_distance=five_minute_distance, 
                            previous_five_minute_distance=previous_five_minute_distance
                        )

                # Check for short positions
                if short_pos_qty > 0:
                    if current_time >= self.next_short_tp_update and short_take_profit is not None:
                        self.next_short_tp_update = self.update_take_profit_spread_bybit(
                            symbol=symbol, 
                            pos_qty=short_pos_qty, 
                            short_pos_price=short_pos_price,
                            long_pos_price=long_pos_price,
                            short_take_profit=short_take_profit,
                            long_take_profit=long_take_profit,
                            positionIdx=2, 
                            order_side="buy", 
                            next_tp_update=self.next_short_tp_update,
                            five_minute_distance=five_minute_distance, 
                            previous_five_minute_distance=previous_five_minute_distance
                        )


                self.cancel_entries_bybit(symbol, best_ask_price, moving_averages["ma_1m_3_high"], moving_averages["ma_5m_3_high"])
                # self.cancel_stale_orders_bybit(symbol)

                time.sleep(30)

            elif symbol in rotator_symbols_standardized and symbol not in open_symbols and trading_allowed:

                # Fetch the API data
                api_data = self.manager.get_api_data(symbol)

                # Extract the required metrics using the new implementation
                metrics = self.manager.extract_metrics(api_data, symbol)

                # Assign the metrics to the respective variables
                one_minute_volume = metrics['1mVol']
                five_minute_volume = metrics['5mVol']
                five_minute_distance = metrics['5mSpread']
                trend = metrics['Trend']
                mfirsi_signal = metrics['MFI']
                funding_rate = metrics['Funding']
                hma_trend = metrics['HMA Trend']

                logging.info(f"Managing new rotator symbol {symbol} not in open symbols")

                if trading_allowed:
                    logging.info(f"New position allowed {symbol}")

                    self.initialize_symbol(symbol, total_equity, best_ask_price, self.max_leverage)

                    with self.initialized_symbols_lock:
                        logging.info(f"Initialized symbols: {list(self.initialized_symbols)}")

                    self.set_position_leverage_long_bybit(symbol, long_pos_qty, total_equity, best_ask_price, self.max_leverage)
                    self.set_position_leverage_short_bybit(symbol, short_pos_qty, total_equity, best_ask_price, self.max_leverage)

                    # Update dynamic amounts based on max trade quantities
                    self.update_dynamic_amounts(symbol, total_equity, best_ask_price)

                    long_dynamic_amount, short_dynamic_amount, min_qty = self.calculate_dynamic_amount_v2(symbol, total_equity, best_ask_price, self.max_leverage)

                    position_data = self.retry_api_call(self.exchange.get_positions_bybit, symbol)

                    long_pos_qty = position_details.get(symbol, {}).get('long', {}).get('qty', 0)
                    short_pos_qty = position_details.get(symbol, {}).get('short', {}).get('qty', 0)


                    should_short = self.short_trade_condition(best_ask_price, moving_averages["ma_3_high"])
                    should_long = self.long_trade_condition(best_bid_price, moving_averages["ma_3_low"])

                    self.bybit_initial_entry_with_qfl_mfi_and_eri(open_orders, symbol, trend, hma_trend, mfirsi_signal, eri_trend, five_minute_volume, five_minute_distance, min_vol, min_dist, long_dynamic_amount, short_dynamic_amount, long_pos_qty, short_pos_qty, should_long, should_short)
                    
                    time.sleep(10)
                else:
                    logging.warning(f"Potential trade for {symbol} skipped as max symbol limit reached.")


            symbol_data = {
                'symbol': symbol,
                'min_qty': min_qty,
                'current_price': current_price,
                'balance': total_equity,
                'available_bal': available_equity,
                'volume': five_minute_volume,
                'spread': five_minute_distance,
                'trend': trend,
                'long_pos_qty': long_pos_qty,
                'short_pos_qty': short_pos_qty,
                'long_upnl': long_upnl,
                'short_upnl': short_upnl,
                'long_cum_pnl': cum_realised_pnl_long,
                'short_cum_pnl': cum_realised_pnl_short,
                'long_pos_price': long_pos_price,
                'short_pos_price': short_pos_price
            }

            shared_symbols_data[symbol] = symbol_data

            if self.config.dashboard_enabled:
                data_to_save = copy.deepcopy(shared_symbols_data)
                with open(dashboard_path, "w") as f:
                    json.dump(data_to_save, f)
                self.update_shared_data(symbol_data, open_position_data, len(open_symbols))

            time.sleep(30)