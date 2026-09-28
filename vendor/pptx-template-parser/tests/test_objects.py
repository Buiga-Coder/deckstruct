from copy import deepcopy
from unittest import TestCase
from pydantic import ValidationError
from template_parser.objects import ExtractedObject, validate_object_links


class ObjectTests(TestCase):
    def fixture(self):
        return dict(id='s1_o1', shape_id=1, name='Content', kind='shape', parent_id=None,
                    z_order=0, geometry=dict(x=0,y=0,width=100,height=50,rotation=0.0,
                    units='EMU',coordinate_space='slide'), source_part='ppt/slides/slide1.xml',
                    xml='<shape/>', text='Revenue increased by 20%', paragraphs=[dict(
                    text='Revenue increased by 20%',level=0,runs=[dict(text='Revenue increased by 20%',
                    font_name=None,font_size_pt=None,bold=None,italic=None)])], assets=[],
                    origin=dict(scope='slide',part_id='s1',source_part='ppt/slides/slide1.xml'))

    def test_populated_text_and_nullable_inherited_geometry(self):
        obj = self.fixture()
        obj['geometry']['width'] = None
        parsed = ExtractedObject.model_validate(obj)
        self.assertEqual(parsed.text, 'Revenue increased by 20%')
        validate_object_links([parsed])

    def test_types_extra_fields_and_units_rejected(self):
        for key,value in [('width','100'),('height',-1),('units','px')]:
            obj=self.fixture()
            obj['geometry'][key]=value
            with self.assertRaises(ValidationError):
                ExtractedObject.model_validate(obj)
        obj=self.fixture(); obj['unexpected']=True
        with self.assertRaises(ValidationError): ExtractedObject.model_validate(obj)

    def test_missing_parent_and_duplicates(self):
        obj=self.fixture(); obj['parent_id']='g'; obj['geometry']['coordinate_space']='g'
        with self.assertRaises(ValueError): validate_object_links([ExtractedObject.model_validate(obj)])
        parsed=ExtractedObject.model_validate(self.fixture())
        with self.assertRaises(ValueError): validate_object_links([parsed,parsed])

    def test_wrong_table_and_origin(self):
        obj=self.fixture(); obj['kind']='table'
        with self.assertRaises(ValidationError): ExtractedObject.model_validate(obj)
        obj=self.fixture(); obj['origin']['source_part']='other.xml'
        with self.assertRaises(ValidationError): ExtractedObject.model_validate(obj)
