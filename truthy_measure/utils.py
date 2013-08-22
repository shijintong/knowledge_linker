import numpy as np
import scipy.sparse as sp
from collections import defaultdict
from cStringIO import StringIO
from tables import Filters, open_file
from tempfile import NamedTemporaryFile

# for closure/closure_rec/c_closure/c_closure_rec
# NOTE: size 10K seems to produce reasonably sized file at the best speed
CHUNKSHAPE = (1,10000)

# dtype for saving COO sparse matrices
coo_dtype = np.dtype([('row', np.int32), ('col', np.int32), ('weight', np.float)])

def arrayfile(data_file, shape, descr, fortran=False):
    '''
    Returns an array that is memory-mapped to an NPY (v1.0) file

    Arguments
    ---------
    data_file - a file-like object opened with write mode
    shape - shape of the ndarray
    descr - any argument that numpy.dtype() can take
    fortran - if True, the array uses Fortran data order, otherwise C order
    '''
    from numpy.lib import format
    header = {
        'descr' : descr,
        'fortran_order' : fortran,
        'shape' : shape
        }
    preamble = '\x93NUMPY\x01\x00'
    data_file.write(preamble)
    cio = StringIO()
    format.write_array_header_1_0(cio, header) # write header here first
    format.write_array_header_1_0(data_file, header) # write header
    cio.seek(0)
    offset = len(preamble) + len(cio.readline()) # get offset
    return np.memmap(data_file, dtype=np.dtype(descr), mode=data_file.mode,
            shape=shape, offset=offset)

def disttosim(x):
    '''
    Transforms a vector non-negative integer distances x to proximity/similarity
    weights in the [0,1] interval:
                                         1
                                   s = -----
                                       x + 1
    Parameters
    ----------
    x : array_like
        an array of non-negative distances.
    '''
    return (x + 1) ** -1

def indegree(adj):
    '''
    Computes the in-degree of each node.

    Parameters
    ----------
    adj : sparse matrix
        the adjacency matrix, in sparse format (see `scipy.sparse`).

    Returns
    -------
    indeg : 1-D array
        the in-degree of each node
    '''
    adj = adj.tocsc()
    indegree = adj.sum(axis=0)
    return np.asarray(indegree).flatten()

def recstosparse(coords, shape=None, fmt='csr'):
    '''
    Returns a sparse adjancency matrix from a records array of (col, row,
    weights)

    Parameters
    ----------
    coords : array_likey
        either a recarray or a 2d ndarray. If recarray, fields must be named:
        `col`, `row`, and `weight`.
    shape : tuple
        the shape of the array, optional.
    fmt : string
        the sparse matrix format to use. See `scipy.sparse`. Default: csr.

    Returns
    -------
    adj : see sparse matrix (see `scipy.sparse`)
        the adjacency matrix in sparse matrix format.
    '''
    if coords.dtype.names is not None:
        # recarray
        irow = coords['row']
        icol = coords['col']
        w = coords['weight']
    else:
        # plain ndarray
        if coords.ndims != 2:
            raise ValueError('expecting a 2-d array or a recarray')
        if coords.shape[1] != 3:
            raise ValueError('expecting three columns (row, col, weights)')
        irow = coords[:,0]
        icol = coords[:,1]
        w = coords[:,2]
    adj = sp.coo_matrix((w, (irow, icol)), shape=shape)
    return adj.asformat(fmt)

def make_weighted(path, N):
    '''
    Return a weighted adjacency matrix, with edge weights computed as the
    in-degree of the incoming vertex, transformed to similarity scores.

    Parameters
    ----------
    path : string
        path to data file.
    N : integer
        number of nodes

    Returns
    -------
    adj : `scipy.sparse.csr_matrix`
        the weighted adjancency matrix
    '''
    # load coordinates from file.
    # coords is a recarray with records (row, col, weights)
    coords = np.load(path)
    # shortcuts
    irow = coords['row']
    icol = coords['col']
    shape = (N,) * 2
    # create sparse adjacency matrix
    adj = recstosparse(coords, shape)
    # computes distances based on in-degrees
    dist = indegree(adj)
    # transform distances to similarity scores
    sim = disttosim(dist)
    # assign the weight to each edge (the weight of an edge is the in-degree of
    # the incoming vertex, translated to a similarity score)
    weights = sim[icol]
    # recreate the sparse matrix with weights and convert to CSR format
    adj = sp.coo_matrix((weights, (irow, icol)), shape=shape)
    adj = adj.tocsr()
    return adj

def dict_of_dicts_to_coo(dd, num=-1):
    '''
    Transforms a dict of dicts to a records array in COOrdinates format.

    Parameters
    ----------
    dd : dict of dicts

    num : int
        the number of coordinate entries

    Returns
    -------
    coo : recarray
        A records array with fields: row, col, and data. This can be used to
        create a sparse matrix. See `coo_dtype`, `scipy.sparse.coo_matrix`.
    '''

    def coorditer(dd):
        for irow in sorted(dd):
            d = dd[irow]
            for icol in sorted(d):
                yield irow, icol, d[icol]
    return np.fromiter(coorditer(dd), coo_dtype, count=num)

def dict_of_dicts_to_ndarray(dd, size):
    '''
    Transforms a dict of dicts to 2-D array

    Parameters
    ----------
    dd : dict of dicts
        a dictionary of dictionary; a dictionary with len(dd) is mapped to each
        key in dd.
    size : tuple
        dimensions of the output array

    Returns
    -------
    a 2-D ndarray. The dtype is inferred from the first element. Missing
    elements are equal to zero.
    '''
    if len(size) != 2:
        raise ValueError('can return only 2-D arrays')
    dty = type(dd.itervalues().next().itervalues().next())
    tmp = np.zeros(size, dtype=dty)
    for irow in dd:
        d = dd[irow]
        for icol in d:
            tmp[irow, icol] = d[icol]
    return tmp

# TODO use heapq with time() as priority

class Cache(dict):
    def __init__(self, maxsize, *args, **kwargs):
        '''
        A dictionary with maximum capacity. Oldest items are removed first,
        using a queue. Whenever a key is gotten from the dictionary, its
        position in the queue is reset to zero.

        Parameters
        ----------
        maxsize : integer
            The maximum size of the cache. When reached, the oldest element in
            the queue is removed.

        Additional arguments are assigned to the dictionary.

        Notes
        -----
        Probably not thread-safe.
        '''
        super(Cache, self).__init__()
        self.maxsize = maxsize
        self._queue = []
        for k, v in args:
            self.__setitem__(k, v)
        for k in kwargs:
            self.__setitem__(k, kwargs[k])
    def __repr__(self):
        return '<{}({}) at 0x{:x}>'.format(self.__class__.__name__,
                self.maxsize, id(self))
    def __setitem__(self, key, value):
        # if key queue is full, pop the oldest item
        if key not in self._queue and len(self._queue) == self.maxsize:
            oldest_key = self._queue.pop(0)
            del self[oldest_key]
        super(Cache, self).__setitem__(key, value)
        # update position of key
        try:
            self._queue.remove(key)
        except ValueError:
            pass
        self._queue.append(key)
    def __getitem__(self, key):
        value = super(Cache, self).__getitem__(key)
        self._queue.remove(key)
        self._queue.append(key)
        return value

def mkcarray(name, shape, chunkshape, atom, outpath=None, ondisk=False):
    '''
    Creates a compressed chunked array.

    Parameters
    ----------
    name : string
        the name of the array in the HDF5 file
    shape : tuple
        shape of the array
    atom : object
        the atom instance of the array data. See `tables.Atom`
    outpath : string
        optional; path to HDF5 file. If None, the array is stored in a temporary
        file that will deleted upon closing it.
    ondisk : bool
        optional; by default the HDF5 file is created in memory and synced to
        disk only when closed.

    Returns
    -------
    arr : tables.CArray instance
        the chunked array
    '''
    if outpath is None:
        outfile = NamedTemporaryFile(suffix='.h5', delete=True)
        outpath = outfile.name
    if ondisk:
        h5f = open_file(outpath, 'w')
    else:
        h5f = open_file(outpath, 'w', driver="H5FD_CORE")
    filters = Filters(complevel=5, complib='zlib')
    arr = h5f.create_carray(h5f.root, name, atom, shape, filters=filters,
            chunkshape=chunkshape)
    return arr

def loadadjcsr(path):
    a = np.load(path)
    indices = a['indices']
    indptr = a['indptr']
    n_rows = len(indptr) - 1
    n_data = len(indices)
    if 'data' not in a.files:
        data = np.ones((n_data,), dtype=np.float64)
    return sp.csr_matrix((data, indices, indptr), shape=(n_rows, n_rows))
