DEBUG = False

def parse(javascript_contents, encoding='ascii'):
	"""
	Given some javascript contents, as a unicode string, return the lxml representation.
	"""
	smjs_javascript = smjs_parse(javascript_contents)
	dictnode_javascript = smjs_to_dictnode(javascript_contents, smjs_javascript)
	dictnode_javascript = fixup_hierarchy(dictnode_javascript)
	dictnode_javascript = calculate_text(javascript_contents, dictnode_javascript)

	from refactorlib.parse import dictnode_to_lxml
	return dictnode_to_lxml(dictnode_javascript, encoding=encoding)

def smjs_parse(javascript_contents):
	from refactorlib import TOP
	from os.path import join
	from subprocess import Popen, PIPE
	from simplejson import loads
	from simplejson.ordered_dict import OrderedDict
	tokenizer_script = join(TOP, 'javascript/tokenize.js')

	smjs = Popen(['smjs', tokenizer_script], stdin=PIPE, stdout=PIPE)
	json = smjs.communicate(javascript_contents)[0]
	tree = loads(json, object_pairs_hook=OrderedDict)

	try:
		last_newline = javascript_contents.rindex('\n')
	except ValueError:
		last_newline = 0

	# smjs is sometimes negelectful of trailing whitespace.
	tree['loc']['end']['line'] = javascript_contents.count('\n') + 1
	tree['loc']['end']['column'] = len(javascript_contents) - last_newline

	return tree

def calculate_text(contents, tree):
	"""
	We do a pre+post order traversal of the tree to calculate the text and tail
	of each node
	"""
	pre, post = 'pre', 'post'
	index = 0
	prev_node = DictNode(name='ROOT', start=0, end=-1)
	stack = [(tree, post), (tree, pre)]
	while stack:
		node, time = stack.pop()
		if time is pre:
			nextindex = node['start']
			if prev_node is node['parent']:
				# First child.
				target = 'text'
			else:
				# Finish up previous sibling
				target = 'tail'
	
			for child in reversed(node['children']):
				stack.extend( ((child, post), (child, pre)) )
		elif time is post:
			nextindex = node['end']
			if prev_node is node:
				# Node has no children.
				target = 'text'
			else:
				# Finish up after last child.
				target = 'tail'

		prev_node[target] = contents[index:nextindex]
		if DEBUG:
			print '%-4s %s' % (time, node)
			print '     %s.%s = %r' % (prev_node, target, prev_node[target])

		# Get ready for next iteration
		index = nextindex
		prev_node = node

	# The top-level node cannot have a tail
	assert not tree.get('tail')
	tree['tail'] = None
	return tree

class DictNode(dict):
	__slots__ = ()
	def __str__(self):
		return '%s(%s-%s)' % (self['name'], self['start'], self['end'])

def smjs_to_dictnode(javascript_contents, tree):
	"""
	Transform a smjs structure into a dictnode, as defined by dictnode_to_lxml.
	This is not a complete transformation. In particular, the nodes have no
	text or tail, and may have some overlap issues.
	"""
	from types import NoneType

	root_dictnode = DictNode(parent=None)
	stack = [(tree, root_dictnode)]
	lines = [len(line)+1 for line in javascript_contents.split('\n')]

	while stack:
		node, dictnode = stack.pop()
			
		children = []
		attrs = {}
		for attr, val in node.items():
			if attr in ('loc', 'type'):
				continue
			elif isinstance(val, list):
				children.extend(val)
			elif isinstance(val, dict) and 'loc' in val:
				if val.get('loc'):
					children.append(val)
				else:
					attrs[val['type']] = val['name']
			elif attr == 'value':
				attrs[attr] = unicode(val)
				# We would normally lose this type information, as lxml
				# wants everything to be a string.
				attrs['type'] = type(val).__name__
			elif isinstance(val, unicode):
				attrs[attr] = val
			elif isinstance(val, (bool, NoneType, str)):
				# TODO: figure out what happens with non-ascii data.
				attrs[attr] = unicode(val)
			else: # Should never happen
				import pudb; pudb.set_trace()

		dictnode.update(dict(
			name=node['type'],
			start=sum(lines[:node['loc']['start']['line']-1]) + node['loc']['start']['column'],
			end=sum(lines[:node['loc']['end']['line']-1]) + node['loc']['end']['column'],
			children=[DictNode(parent=dictnode) for child in children],
			attrs=attrs,
		))
		stack.extend(reversed(zip(children, dictnode['children'])))
	return root_dictnode

def fix_parentage(node, parent):
	"""We fix nodes whose children overlap their boundaries by widening the parent"""
	orig_parent = parent
	while parent is not None and node['start'] >= parent['end']:
		parent = parent['parent']

	if parent is orig_parent:
		return False
	else:
		# This node needs re-parenting.
		if DEBUG: print '  Re-parenting %s: old:%s  new:%s' % (node, node['parent'], parent)
		orig_parent['children'].remove(node)
		for index, sibling in enumerate(parent['children']):
			if (sibling['start'], sibling['end']) > (node['start'], node['end']):
				parent['children'].insert(index, node)
				break
		else:
			parent['children'].append(node)
		node['parent'] = parent
		return True

def fix_overlap(node, parent, index):
	"""
	This function only modifies the input `node`, but will sometimes re-parent the node, when necessary.
	The node will only be moved "further" in the tree, in depth-first order.
	Returns True if the node was reparented.
	"""
	assert not node['end'] <= parent['start'], "Node ends before parent: %s-%s" % (parent, node)
	if node['start'] < parent['start']:
		if DEBUG: print '    node starts too soon %s: %s ->' % (parent, node),
		node['start'] = parent['start']
		if DEBUG: print node
	if fix_parentage(node, parent):
		return True
	if node['end'] > parent['end']:
		if DEBUG: print '    node ends too late %s: %s ->' % (parent, node),
		node['end'] = parent['end']
		if DEBUG: print node
	if index >= 1:
		prev_node = parent['children'][index-1]
		if prev_node['start'] >= node['start']:
			if DEBUG: print '    node starts before previous sibling %s-%s: %s ->' % (parent, prev_node, node),
			node['start'] = prev_node['end']
			if DEBUG: print node
	try:
		next_node = parent['children'][index+1]
	except IndexError:
		pass
	else:
		if node['start'] >= next_node['start']:
			# That node will fix itself.
			pass
		elif node['end'] > next_node['start']:
			if DEBUG: print '    node ends after next sibling starts %s-%s: %s ->' % (parent, next_node, node),
			node['end'] = next_node['start']
			if DEBUG: print node

	assert node['start'] <= node['end'], "Negative-width node: %s" % node

def fixup_hierarchy(tree):
	# We traverse the tree in a depth-first manner, taking care not to take
	# copies of the 'children' lists, nor iterate directly over them.
	stack = [(tree,0)]
	while stack:
		parent, index = stack.pop()
		try:
			node = parent['children'][index]
		except IndexError:
			continue

		if DEBUG: print node
		if fix_overlap(node, parent, index):
			# That node got repositioned further down the depth-first order. Retry.
			stack.append((parent, index))
		else:
			stack.append((parent, index+1))
			stack.append((node, 0))
	return tree
