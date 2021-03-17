from core.vectorizers import SentBERTVectorizer
from core.vectorizers import CPCVectorizer
from core.index_selection import SublassesBasedIndexSelector
from core.filters import FilterArray, PublicationDateFilter, DocTypeFilter
from core.obvious import Combiner
from core.indexes import IndexesDirectory
from core.search import VectorIndexSearcher
from core.documents import Document, Patent
from core.snippet import SnippetExtractor, CombinationalMapping
from core.reranking import ConceptMatchRanker
from core.encoders import default_boe_encoder
from core.datasets import PoC
from core.results import SearchResult
from core.encoders import default_embedding_matrix
import copy
import io
import re
import cv2
import os
import markdown

import boto3
import botocore.exceptions
s3 = boto3.resource('s3')
from config.config import PQAI_S3_BUCKET_NAME

from PIL import Image
import core.remote as remote
import core.utils as utils

from config.config import indexes_dir, reranker_active, index_selection_disabled
from config.config import allow_outgoing_extension_requests
from config.config import allow_incoming_extension_requests
from config.config import docs_file

vectorize_text = SentBERTVectorizer().embed
available_indexes = IndexesDirectory(indexes_dir)
select_indexes = SublassesBasedIndexSelector(available_indexes).select
vector_search = VectorIndexSearcher().search
extract_snippet = SnippetExtractor.extract_snippet
generate_mapping = SnippetExtractor.map
reranker = None if not reranker_active else ConceptMatchRanker()


class APIRequest():
    
    def __init__(self, req_data=None):
        self._data = req_data
        self._validate()

    def serve(self):
        try:
            response = self._serving_fn()
            return self._formatting_fn(response)
        except BadRequestError as e:
            raise e
        except ResourceNotFoundError as e:
            raise e
        except:
            raise ServerError()

    def _validate(self):
        self._validation_fn()

    def _serving_fn(self):
        pass

    def _validation_fn(self):
        pass

    def _formatting_fn(self, response):
        return response


class BadRequestError(Exception):

    def __init__(self, msg='Invalid request.'):
        self.message = msg


class ServerError(Exception):

    def __init__(self, msg='Server error while handling request.'):
        self.message = msg


class NotAllowedError(Exception):

    def __init__(self, msg="Request disallowed."):
        self.message = msg


class ResourceNotFoundError(Exception):

    def __init__(self, msg="Resource not found."):
        self.message = msg


class SearchRequest(APIRequest):

    _name = 'Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)
        self._query = req_data.get('q', '')
        self._latent_query = req_data.get('lq', '')
        self._full_query = self._get_full_query()

        self._offset = max(0, int(req_data.get('offset', 0)))
        self._n_results = int(req_data.get('n', 10))
        self._n_results += self._offset # for pagination
        
        self._indexes = self._get_indexes()
        self._need_snippets = self._read_bool_value('snip')
        self._need_mappings = self._read_bool_value('maps')
        self._filters = FilterExtractor(self._data).extract()
        self.MAX_RES_LIMIT = 500

    def __repr__(self):
        return f'{self._name}: {json.dumps(self._data)}'

    def __str__(self):
        return f'[{self._name}]'

    def _serving_fn(self):
        return self._searching_fn()

    def _searching_fn(self):
        pass

    def _get_full_query(self):
        return (self._query + '\n' + self._latent_query).strip()

    def _get_indexes(self):
        if self._index_specified_in_request():
            index_in_req = self._data['idx']
            return available_indexes.get(index_in_req)
        elif index_selection_disabled:
            return list(available_indexes.available())
        else:
            return select_indexes(self._full_query, 3)

    def _index_specified_in_request(self):
        req_data = self._data
        if not 'idx' in req_data:
            return False
        if req_data['idx'] == 'auto':
            return False
        return True

    def _read_bool_value(self, key):
        val = self._data.get(key)
        if ((isinstance(val, str) and val in ['true', '1', 'yes']) or
            (isinstance(val, int) and val != 0)):
            return True
        return False

    def _validation_fn(self):
        if not 'q' in self._data:
            raise BadRequestError(
                'Request does not contain a query.')

    def _add_snippet_if_needed(self, result):
        if self._need_snippets:
            result.snippet = SnippetExtractor.extract_snippet(self._query, result.full_text)


class FilterExtractor():

    def __init__(self, req_data):
        self._data = req_data

    def extract(self):
        filters = FilterArray()
        date_filter = self._get_date_filter()
        doctype_filter = self._get_doctype_filter()
        if date_filter:
            filters.add(date_filter)
        if doctype_filter:
            filters.add(doctype_filter)
        return filters

    def _get_date_filter(self):
        after = self._data.get('after', None)
        before = self._data.get('before', None)
        if after or before:
            after = None if not bool(after) else after
            before = None if not bool(before) else before
            return PublicationDateFilter(after, before)

    def _get_doctype_filter(self):
        doctype = self._data.get('type')
        if doctype:
            return DocTypeFilter(doctype)


class SearchRequest102(SearchRequest):

    _name = '102 Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)

    def _searching_fn(self):
        results = self._get_results()
        results = self._rerank(results)
        results = results[:self._n_results]
        return results[self._offset:]

    def _get_results(self):
        qvec = vectorize_text(self._full_query)

        """
        During reranking, lower ranked results may come up on top of the list.
        This means that the first 10 results a user sees when only 10 results 
        are search may differ from the first 10 results when, say, 50 results
        are searched. To mitigate this issue, always search with at least 50
        results.
        """
        n = max(50, self._n_results)

        results = []
        m = n
        while len(results) < n and m < self.MAX_RES_LIMIT:
            results = vector_search(qvec, self._indexes, m)
            results = self._filters.apply(results)
            m *= 2
        return results

    def _add_remote_results_to(self, local_results):
        if not allow_outgoing_extension_requests:
            return local_results
        remote_results = remote.search_extensions(self._data)
        return remote.merge([local_results, remote_results])

    def _rerank(self, results):
        if not reranker:
            return results
        result_texts = [r.abstract for r in results]
        ranks = reranker.rank(self._query, result_texts)
        return [results[i] for i in ranks]

    def _formatting_fn(self, results):
        for result in results:
            self._add_snippet_if_needed(result)
            self._add_mapping_if_needed(result)
            self._add_drawing_link(result)
        results = [res.json() for res in results]
        results = self._add_remote_results_to(results)
        return {
            'results': results,
            'query': self._query,
            'latent_query': self._latent_query }

    def _add_mapping_if_needed(self, result):
        if self._need_mappings:
            try:
                result.mapping = generate_mapping(self._query, result.full_text)
            except:
                result.mapping = None

    def _add_drawing_link(self, result):
        if result.is_patent():
            result.image = f'https://api.projectpq.ai/patents/{result.id}/thumbnails/1'


class SearchRequest103(SearchRequest):

    _name = '103 Search Request'

    def __init__(self, req_data):
        super().__init__(req_data)

    def _searching_fn(self):
        docs = self._get_docs_to_combine()
        abstracts = [doc.abstract for doc in docs]
        combiner = Combiner(self._query, abstracts)
        n = max(50, self._n_results) # see SearchRequest102 for why max used
        index_pairs = combiner.get_combinations(n)
        combinations = [(docs[i], docs[j]) for i, j in index_pairs]
        combinations = combinations[:self._n_results]
        return combinations[self._offset:]

    def _get_docs_to_combine(self):
        params = self._get_interim_request_params()
        interim_req = SearchRequest102(params)
        results = interim_req.serve()['results']
        return [SearchResult(r['id'], r['index'], r['score']) for r in results]

    def _get_interim_request_params(self):
        params = self._data.copy()
        params['n'] = 100
        params['maps'] = 0
        params['snip'] = 0
        params['offset'] = 0
        return params

    def _formatting_fn(self, combinations):
        for combination in combinations:
            for result in combination:
                self._add_snippet_if_needed(result)
            self._add_mapping_if_needed(combination)
        return {
            'results': [[r.json() for r in c] for c in combinations],
            'query': self._query,
            'latent_query': self._latent_query }

    def _add_mapping_if_needed(self, combination):
        if not self._need_mappings:
            return
        for result in combination:
            try:
                result.mapping = generate_mapping(self._query, result.full_text)
            except:
                result.mapping = None


class SimilarPatentsRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._pn = req_data.get('pn')

    def _serving_fn(self):
        search_request = self._create_text_query_request()
        return SearchRequest102(search_request).serve()

    def _create_text_query_request(self):
        claim = Patent(self._pn).first_claim
        query = utils.remove_claim_number(claim)
        search_request = self._data.copy()
        search_request['q'] = query
        search_request.pop('pn')
        return search_request

    def _validation_fn(self):
        if not utils.is_patent_number(self._data.get('pn')):
            raise BadRequestError(
                'Request does not contain a valid patent number.')

    def _formatting_fn(self, response):
        response['query'] = self._pn
        return response


class PatentPriorArtRequest(SimilarPatentsRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._before = Patent(self._pn).filing_date

    def _serving_fn(self):
        search_request = self._create_text_query_request()
        search_request['before'] = self._before
        return SearchRequest102(search_request).serve()


class DocumentRequest(APIRequest):

    _name = 'Document Request'

    def __init__(self, req_data):
        super().__init__(req_data)
        self._doc_id = req_data['id']

    def _validation_fn(self):
        if not 'id' in self._data:
            raise BadRequestError(
                'Request does not contain a document ID.')

    def _serving_fn(self):
        return Document(self._doc_id).json()


class PassageRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._query = req_data.get('q')
        self._doc_id = req_data.get('pn')
        self._doc = Document(self._doc_id)

    def _validation_fn(self):
        if not self._data.get('q'):
            raise BadRequestError(
                'Request does not contain a query.')
        if not self._data.get('pn'):
            raise BadRequestError(
                'Request does not specify a document.')


class SnippetRequest(PassageRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        query = self._query
        text = self._doc.full_text
        return SnippetExtractor().extract_snippet(query, text)

    def _formatting_fn(self, snippet):
        return {
            'query': self._query,
            'id': self._doc_id,
            'snippet': snippet }


class MappingRequest(PassageRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        query = self._query
        text = self._doc.full_text
        return generate_mapping(query, text)

    def _formatting_fn(self, mapping):
        return {
            'query': self._query,
            'id': self._doc_id,
            'mapping': mapping }


class DatasetSampleRequest(APIRequest):

    poc_dataset = PoC()

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        name = self._data['dataset']
        if name.lower() != 'poc':
            raise ResourceNotFoundError(f'No dataset named {name}.')
        n = self._data['n']
        return self.poc_dataset[int(n)]

    def _validation_fn(self):
        if not 'dataset' in self._data:
            raise BadRequestError('Dataset name unspecified.')
        if not 'n' in self._data:
            raise BadRequestError('Sample number unspecified.')

    def _formatting_fn(self, sample):
        formatted = {}
        formatted['anc'] = self._format(sample['anc'])
        formatted['pos'] = self._format(sample['pos'])
        formatted['negs'] = [self._format(neg) for neg in sample['negs']]
        return formatted

    def _format(self, pn):
        patent = Patent(pn)
        return {
            'publicationNumber': patent.id,
            'title': patent.title,
            'abstract': patent.abstract
        }


class IncomingExtensionRequest(SearchRequest102):

    def __init__(self, req_data):
        if not allow_incoming_extension_requests:
            raise NotAllowedError(
                'Server does not accept extension requests.')
        else:
            super().__init__(req_data)


class AbstractPatentDataRequest(APIRequest):

    PN_PATTERN = r'^US(RE)?\d{4,11}[AB]\d?$'

    def __init__(self, req_data):
        super().__init__(req_data)
        self._pn = req_data['pn']
        self._patent = Patent(self._pn)

    def _validation_fn(self):
        if self._data['pn'][:2] != 'US':
            raise BadRequestError('Only US patents supported.')
        if not re.match(self.PN_PATTERN, self._data['pn']):
            raise BadRequestError('Could not parse patent number.')

    def _is_granted_patent(self):
        return len(self._pn) < 13

    def _formatting_fn(self, response):
        if isinstance(response, dict):
            response['pn'] = self._patent.publication_id
        return response


class AbstractDrawingRequest(AbstractPatentDataRequest):
    
    S3_BUCKET = s3.Bucket(PQAI_S3_BUCKET_NAME)

    def __init__(self, req_data):
        super().__init__(req_data)

    def _get_prefix(self):
        number = self._get_8_digits() if self._is_granted_patent() else self._pn
        return f'images/{number}-'

    def _get_8_digits(self):
        pattern = r'(.+?)(A|B)\d?'
        digits = re.match(pattern, self._pn[2:])[1]    # extract digits
        if len(digits) == 7:
            digits = '0' + digits
        return digits


class DrawingRequest(AbstractDrawingRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._n = req_data['n']
        self._tmp_file = None
        self._local_file = None
        self._filename = None

    def _validation_fn(self):
        super()._validation_fn()
        if not re.match(r'\d+', str(self._data['n'])):
            raise BadRequestError('Drawing number should be integer.')

    def _serving_fn(self):
        self._download_file_from_s3()
        self._convert_to_jpg()
        return self._local_file

    def _download_file_from_s3(self):
        s3_prefix = self._get_prefix()
        s3_suffix = f'{self._n}.tif'
        s3_key = s3_prefix + s3_suffix
        filename_with_ext = s3_key.split('/')[-1]
        self._filename = filename_with_ext.split('.')[0]
        self._tmp_file = f'/tmp/{self._filename}.tif'
        try:
            self.S3_BUCKET.download_file(s3_key, self._tmp_file)
        except botocore.exceptions.ClientError:
            raise ResourceNotFoundError('Drawing unavailable.')

    def _convert_to_jpg(self):
        im = Image.open(self._tmp_file)
        self._local_file = f'/tmp/{self._filename}.jpg'
        im.convert("RGB").save(self._local_file, "JPEG", quality=50)
        os.remove(self._tmp_file)
        return self._local_file


class ListDrawingsRequest(AbstractDrawingRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        prefix = self._get_prefix()
        indexes = [o.key.split('-')[-1].split('.')[0]
                     for o in self.S3_BUCKET.objects.filter(Prefix=prefix)]
        return {'drawings': indexes}


class PatentDataRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        patent_data = {
            'title': self._patent.title,
            'abstract': self._patent.abstract,
            'description': self._patent.description,
            'claims': self._patent.claims
        }
        return patent_data
        

class TitleRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'title': self._patent.title}


class AbstractRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'abstract': self._patent.abstract}


class AllClaimsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'claims': self._patent.claims}


class OneClaimRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._n = req_data['n']

    def _validation_fn(self):
        super()._validation_fn()
        if not 'n' in self._data:
            raise BadRequestError('Claim number unspecified.')
        if not isinstance(self._data['n'], int):
            raise BadRequestError('Claim number should be integer')
        if self._data['n'] < 1:
            raise BadRequestError('Claim number cannot be <= 0')

    def _serving_fn(self):
        if self._n > len(self._patent.claims):
            raise BadRequestError(f'{self._pn} has no claim #{self._n}')
        return {
            'claim': self._patent.claims[self._n-1],
            'claim_num': self._n
        }


class IndependentClaimsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'claims': self._patent.independent_claims}


class PatentDescriptionRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'description': self._patent.description}


class CitationsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        back_cits = self._patent.backward_citations
        for_cits = self._patent.forward_citations
        return {
            'citations_backward': back_cits,
            'citations_forward': for_cits
        }


class BackwardCitationsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        cits = self._patent.backward_citations
        return {'citations_backward': cits}


class ForwardCitationsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        cits = self._patent.forward_citations
        return {'citations_forward': cits}


class ConceptsRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._text = req_data['text']

    def _validation_fn(self):
        if not isinstance(self._data['text'], str):
            raise BadRequestError('Invalid text.')
        if not self._data['text'].strip():
            raise BadRequestError('No text to work with.')

    def _serving_fn(self):
        return list(default_boe_encoder.encode(self._text))


class AbstractConceptsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        req = ConceptsRequest({'text': self._patent.abstract})
        concepts = req.serve()
        return {'concepts': concepts}


class DescriptionConceptsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        req = ConceptsRequest({'text': self._patent.description})
        concepts = req.serve()
        return {'concepts': concepts}


class CPCsRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        return {'cpcs': self._patent.cpcs}


class ListThumbnailsRequest(ListDrawingsRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        thumbnails = super()._serving_fn()['drawings']
        return {'thumbnails': thumbnails}


class ThumbnailRequest(DrawingRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._h = 200
        self._w = None

    def _serving_fn(self):
        im_path = super()._serving_fn()
        im = cv2.imread(im_path)
        im = self._do_scaling(im)
        cv2.imwrite(im_path, im) # Overwrite the original
        return im_path

    def _do_scaling(self, im):
        h, w = self._get_out_dims(im)
        im = cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)
        return im

    def _get_out_dims(self, im):
        h, w, channels = im.shape
        r = w / h
        if isinstance(self._h, int) and isinstance(self._w, int):
            return (self._h, self._w)
        elif isinstance(self._h, int):
            width = max(1, int(self._h*r))
            return (self._h, width)
        elif isinstance(self._w, int):
            height = max(1, int(self._w/r))
            return (height, self._w)
        else:
            return (h, w)


class PatentCPCVectorRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        cpcs = self._patent.cpcs
        vector = CPCVectorizer().embed(cpcs)
        return {'vector': vector.tolist()}


class PatentAbstractVectorRequest(AbstractPatentDataRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        abstract = self._patent.abstract
        vector = SentBERTVectorizer().embed(abstract)
        return {'vector': vector.tolist()}


class ConceptRelatedRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)
        self._concept = req_data['concept']

    def _validation_fn(self):
        if not isinstance(self._data['concept'], str):
            raise BadRequestError('Concept must be a string.')
        if not self._data['concept'].strip():
            raise BadRequestError('Null string provided as concept')

    def _formatting_fn(self, response):
        if isinstance(response, dict):
            response['concept'] = self._concept
        return response


class SimilarConceptsRequest(ConceptRelatedRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        if not self._concept in default_embedding_matrix:
            raise ResourceNotFoundError(f'No vector for "{self._concept}"')

        neighbours = default_embedding_matrix.similar_to_item(self._concept)
        return {'similar': neighbours}


class ConceptVectorRequest(ConceptRelatedRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        if not self._concept in default_embedding_matrix:
            raise ResourceNotFoundError(f'No vector for "{self._concept}"')

        vector = default_embedding_matrix[self._concept]
        return {'vector': list(vector)}


class DocumentationRequest(APIRequest):

    def __init__(self, req_data):
        super().__init__(req_data)

    def _serving_fn(self):
        with open(docs_file, 'r') as f:
            md = f.read()
            html = markdown.markdown(md, extensions=['tables', 'toc', 'smarty'])
        return html