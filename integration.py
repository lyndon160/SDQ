import qtest.firsttiercaller as firsttiercaller
import qtest.resourceallocationcaller as secondtiercaller
import pyjsonrpc
import json
import threading
import random
import logging
import calendar
import time

from pygraph.classes.digraph import digraph
from optparse import OptionParser

class Integration(object):

    _testing = True
    first = []
    second = []
    limits = []
    tiers = ["first", "second"]
    _switch_port_results= {}
    _first_tier_result_mapping = ['A', 'B', 'C', 'D', 'E']

    def __init__(self, config, poll, threshold, capacity, host, port):
        self._parse_graph(self._load_graph(config))
        self._controller = self.Controller(host, port, self._testing)
        self._experience = self.Experience()
        self._initialise_switch_ports()
        self._threshold = threshold
        self._poll(float(poll))

    def _load_graph(self, path):
        with open(path) as json_data:
            graph = json.load(json_data)
            json_data.close()
        return graph

    def _parse_graph(self, graph):
        self._graph = digraph()
        self._parse_nodes(graph)
        self._parse_edges(graph)

    def _parse_nodes(self, tree):
        for _type, node in tree["nodes"].iteritems():
            for _id, attrs in node.iteritems():
                attrs["type"] = _type
                self._graph.add_node(node=_id, attrs=attrs.items())
                try:
                    tier = getattr(self, attrs["tier"])
                    tier.append(_id)
                except KeyError:
                    pass

    def _parse_edges(self, tree):
        for _id, attrs in tree["edges"].iteritems():
            self._graph.add_edge(edge=tuple(attrs["items"]), wt=1, label=_id, attrs=attrs.items())
            if "limit" in attrs.keys():
                self.limits.append(tuple(attrs["items"]))

    def _initialise_switch_ports(self):
        for edge in self.limits:
            attrs = dict(self._graph.edge_attributes(edge))
            switch = self._get_field_from_node(attrs["items"][0], "dpid")
            self._controller.call(method="enforce_port_outbound", params=[switch, attrs["port"], attrs["limit"]])

    def _poll(self, poll):
        threading.Timer(poll, self._poll, [poll]).start()
        for tier in self.tiers:
            self._fetch_stats(tier)

    def _get_field_from_node(self, node, field):
        try:
            return dict(self._graph.node_attributes(node))[str(field)]
        except KeyError:
            return {}

    def _get_field_from_edge(self, edge, field):
        try:
            return dict(self._graph.edge_attributes(edge))[str(field)]
        except KeyError:
            return {}

    def _fetch_stats(self, tier):
        nodes = getattr(self, tier)
        for node in nodes:
            switch = self._get_field_from_node(node, "dpid")
            result = self._controller.call(method="report_switch_ports", params=[False, False, switch])
            if self._compare_switch_ports(tier, switch, result):
                 self._recalculate(tier, switch)

    def _compare_switch_ports(self, tier, switch, result):
        if switch not in self._switch_port_results.keys():
            self._switch_port_results[switch] = {}
        for port, throughput in result.iteritems():
            if port not in self._switch_port_results[switch].keys():
                self._switch_port_results[switch][port] = throughput[1] #Should be tx?
                continue
            else:
                current = throughput[1]
                previous = self._switch_port_results[switch][port]
            self._switch_port_results[switch][port] = throughput[1]
            return self._calculate_difference(current, previous)

    def _calculate_difference(self, current, previous):
        difference = current - previous #download B/s
        if abs(difference) >= self._threshold:
            return True

    def _recalculate(self, tier, switch):
        getattr(self, '_recalculate_' + tier + '_tier')(switch)

    def _recalculate_first_tier(self, _):
        totalbw, households = self._fetch_first_tier_stats()
        result = self._experience.first(totalbw=totalbw, households=households)
        result = self._fix_household_result(self.first[0], result)
        self._effect_first_tier_change(self.first[0], result)

    def _recalculate_second_tier(self, switch):
        totalbw, clients, _ = self._fetch_second_tier_stats(switch)
        result = self._experience.second(totalbw=totalbw, clients=clients)
        self._effect_second_tier_change(switch, result)
        self._update_forgiveness_effect(result)

    def _update_forgiveness_effect(self, result):
        timestamp = calendar.timegm(time.gmtime())
        for client_id, allocation in result.iteritems():
            self._experience.forgiveness_effect(client=client_id, timestamp=timestamp, bitrate=allocation[3])

    def _effect_second_tier_change(self, switch, result):
        for id_, allocation in result.iteritems():
            limit = allocation[3]
            node = self._find_node_from_label("dpid", switch)
            port = self._get_field_from_edge((node, id_), "port")
            self._controller.call(method="enforce_port_outbound", params=[switch, port, limit])

    def _effect_first_tier_change(self, switch, result):
        for household in result:
            neighbor = self._get_node_from_label("household", household["households_id"])
            node = self._find_node_from_label("dpid", switch)
            port = self._get_field_from_edge((node, neighbor), "port")
            limit = household["limit"]
          #limit = limit * 125 #Convert from kilobits to bytes
            self._controller.call(method="enforce_port_outbound", params=[switch, port, limit])

    def _fetch_first_tier_stats(self):
        households = []
        totalbw = 0
        background = 0
        neighbors = self._graph.neighbors(self.first[0])
        for node in self.second:
            household_available, _, household_background = self._fetch_second_tier_stats(node, dpid=False)
            background += household_background
            id_ = self._get_field_from_node(node, "household")
            households.append((id_, household_available))
        for neighbor in list(set(neighbors)-set(self.second)):
            port = self._get_field_from_edge((node, neighbor), "port")
            switch = self._get_field_from_node(node, "dpid")
            totalbw += self._controller.call(method="report_port", params=[True, False, switch, port])[3] #Rx - link max
        totalbw += background
        return (totalbw, households)

    def _fetch_second_tier_stats(self, switch, dpid=True):
        clients = []
        totalbw = 0
        if dpid:
            node = self._find_node_from_label("dpid", switch)
        else:
            node = switch
        neighbors = self._classify_neighbors(node)
        for foreground in neighbors["foreground"]:
            clients.append(self._fetch_foreground(node, foreground, switch))
        totalbw, background = self._fetch_switch(node, neighbors["switch"][0], neighbors["background"], switch)
        return (totalbw, clients, background)

    def _classify_neighbors(self, node):
        neighbors = {}
        for neighbor in self._graph.neighbors(node):
            _type = self._get_field_from_node(neighbor, "type")
            if not neighbors.has_key(_type):
                neighbors[_type] = []
            neighbors[_type].append(neighbor)
        return neighbors

    def _fetch_foreground(self, node, neighbor, switch):
        """Assume no background traffic from a foreground node."""
        port = self._get_field_from_edge((node, neighbor), "port")
        available_bandwidth = self._controller.call(method="report_port", params=[True, False, switch, port])[1] #Tx - link max - no background to remove
        resolution = self._get_field_from_node(neighbor, "resolution")
        return ((neighbor, available_bandwidth, resolution))

    def _fetch_switch(self, node, neighbor, background, switch):
        background_traffic = 0
        port = self._get_field_from_edge((node, neighbor), "port")
        max_bandwidth = self._controller.call(method="report_port", params=[True, False, switch, port])[3] #Rx - link max
        for client in background:
            port = self._get_field_from_edge((node, client), "port")
            background_traffic += self._controller.call(method="report_port", params=[False, False, switch, port])[1] #Tx - current background
        available_bandwidth = max_bandwidth - background_traffic
        assert available_bandwidth > 0
        return available_bandwidth, background_traffic

    def _find_node_from_label(self, field, value):
        for node in self._graph.nodes():
            if self._get_field_from_node(node, field) == value:
                return node

    def _fix_household_result(self, switch, result):
        """Map index in result to household ID. Fixed mapping (see object variables)."""
        limits = []
        for index, limit in enumerate(result):
            household_id = self._first_tier_result_mapping[index]
            neighbor = self._find_node_from_label("household", household_id)
            node = self._find_node_from_label("dpid", switch)
            port = self._get_field_from_edge((node, neighbor), "port")
            limits.append({'household_id' : household_id, 'port' : port, 'limit' : limit})
        return limits

    class Controller(object):

        def __init__(self, host, port, testing):
            self._testing = testing
            self._client = pyjsonrpc.HttpClient(url = "http://" + host + ":" + port + "/jsonrpc")

        def call(self, **kwargs):
            result = None
            logging.debug('[controller][call]: %s', kwargs)
            if self._testing:
                if kwargs['method'] == 'report_switch_ports':
                    result = { "1":self._generate_random_bandwidth(4),
                    "2":self._generate_random_bandwidth(4),
                    "3":self._generate_random_bandwidth(4),
                    "4":self._generate_random_bandwidth(4),
                    "5":self._generate_random_bandwidth(4),
                    "6":self._generate_random_bandwidth(4),
                    "7":self._generate_random_bandwidth(4)}
                elif kwargs['method'] == "enforce_service":
                    result = random.randint(53, 200)
                elif kwargs['method'] == "report_port":
                    result = self._generate_random_bandwidth(4)
            else:
		try:
                    result = self._client.call(kwargs['method'], *kwargs['params'])
            	except pyjsonrpc.rpcerror.InternalError:
		    print kwargs
                    result = self._client.notify(kwargs['method'], *kwargs['params'])
		    result = None
	    logging.debug('[controller][result]: %s', result)
            return result

        def _generate_random_bandwidth(self, length):
            _max = 20000000
            _min = 300000
            bandwidth = []
            for _ in range(length):
                bandwidth.append(random.randint(_min, _max))
            return bandwidth

    class Experience(object):

        def __init__(self):
            self.first_tier = firsttiercaller.FirstTier()
            self.second_tier = secondtiercaller.SecondTier()

        def first(self, **kwargs):
            logging.debug('[experience][first][call]: %s', kwargs)
            result = self.first_tier.call(**kwargs)
            logging.debug('[experience][first][result]: %s', result)
            return result

        def second(self, **kwargs):
            logging.debug('[experience][second]: %s', kwargs)
            result = self.second_tier.call(**kwargs)
            logging.debug('[experience][second][result]: %s', result)
            return result

        def forgiveness_effect(self, **kwargs):
            logging.debug('[experience][forgiveness]: %s', kwargs)
            self.second_tier.set_session_index(**kwargs)

if __name__ == '__main__':
    parser = OptionParser()
    parser.add_option("-i", "--interval", dest="interval", help="controller polling interval, measured in seconds", default=5.0)
    parser.add_option("-t", "--threshold", dest="threshold", help="change threshold at which to trigger a recalculation, measured in bytes", default=1000)
    parser.add_option("-c", "--capacity", dest="capacity", help="maximum capacity to initialise meter to, measured in bytes", default=1000000000)
    parser.add_option("-n", "--hostname", dest="host", help="controller hostname", default="localhost")
    parser.add_option("-p", "--port", dest="port", help="controller interface port", default=4000)
    (options, args) = parser.parse_args()
    logging.basicConfig(filename='debug.log',level=logging.DEBUG, format='[%(asctime)s:%(levelname)s]%(message)s')
    integration = Integration("config.json", float(options.interval), int(options.threshold), int(options.capacity), options.host, str(options.port))
